"""
Dependency Analyzer — LangGraph node that applies rule-based detectors to the
resource inventory and produces directed dependency edges.

Edge direction: FROM dependent TO dependency.
Example: Deployment/default/first-agent → virtual/istiod
  means "first-agent depends on istiod"

In this orientation, heavily-depended-on nodes (istiod, redis, metrics-server)
accumulate high in-degree, which the scorer uses for criticality ranking.

Rules implemented (POC subset):
  NS-01  Namespace istio-injection=enabled → Deployments in ns depend on istiod
  AS-01  HPA scaleTargetRef → HPA depends on target Deployment
  AS-02  HPA present → target Deployment depends on metrics-server
  EG-01  AIGatewayRoute parentRefs → Route depends on Gateway
  EG-02  AIGatewayRoute backendRefs → Route depends on AIServiceBackend
  EG-03  AIServiceBackend → depends on matched Envoy Backend CRD
  EG-04  BackendSecurityPolicy targetRefs → BSP depends on AIServiceBackend
  EG-05  BackendSecurityPolicy secretRef → BSP depends on Secret
  SE-AWS ServiceEntry *.amazonaws.com hosts → SE depends on aws/* virtual node
         + Deployments in same namespace depend on the ServiceEntry
  RL-01  BackendTrafficPolicy with rateLimit → depends on Gateway + virtual/redis
  CD-01  ArgoCD Application → AppProject (via spec.project)
  CD-02  Deployments in Application destination.namespace depend on the Application
  LLM-01 Fallback: nodes with zero detected deps + available spec → Claude Sonnet
         via Envoy AI Gateway; returns JSON dependency list tagged source="llm"
"""

import json
import logging
import re

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ── AWS FQDN pattern ─────────────────────────────────────────────────────────
_AWS_PATTERN = re.compile(r'^([a-z0-9-]+)\.[a-z0-9-]+\.amazonaws\.com$')

_AWS_SERVICE_MAP = {
    "bedrock-runtime": "bedrock-runtime",
    "bedrock":         "bedrock",
    "s3":              "s3",
    "secretsmanager":  "secrets-manager",
    "kms":             "kms",
    "sqs":             "sqs",
    "dynamodb":        "dynamodb",
    "logs":            "cloudwatch",
    "monitoring":      "cloudwatch",
    "eks":             "eks",
    "ec2":             "ec2",
    "iam":             "iam",
    "sts":             "sts",
}


def _extract_aws_service(host: str) -> str | None:
    """Parse 'bedrock-runtime.us-east-1.amazonaws.com' → 'bedrock-runtime'."""
    if host.startswith("*"):
        return "amazonaws"
    m = _AWS_PATTERN.match(host)
    if not m:
        return None
    prefix = m.group(1)
    return _AWS_SERVICE_MAP.get(prefix, prefix)


# ── Node ID helpers ───────────────────────────────────────────────────────────

def _node_id(kind: str, namespace: str, name: str) -> str:
    if namespace:
        return f"{kind}/{namespace}/{name}"
    return f"{kind}/{name}"


def _virtual(name: str) -> str:
    return f"virtual/{name}"


def _aws(service: str) -> str:
    return f"aws/{service}"


# ── Node / edge builders ──────────────────────────────────────────────────────

def _make_node(node_id: str, kind: str, namespace: str, name: str,
               labels: dict | None = None) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "labels": labels or {},
    }


# Kinds whose dependencies are fully covered by named rules.
# Nodes of any OTHER kind with zero outbound edges are sent to the LLM fallback.
_RULE_HANDLED_KINDS = frozenset({
    "Deployment", "HorizontalPodAutoscaler",
    "Gateway", "AIGatewayRoute", "AIServiceBackend",
    "BackendSecurityPolicy", "BackendTrafficPolicy", "ServiceEntry",
    "Application", "AppProject",
    "Virtual", "AWS",
})

# Inventory keys that carry full CRD spec (raw dicts from Kubernetes API).
# Used to build the spec map for LLM prompts.
_CRD_INVENTORY_KEYS: dict[str, str] = {
    "gateways":                  "Gateway",
    "ai_gateway_routes":         "AIGatewayRoute",
    "ai_service_backends":       "AIServiceBackend",
    "backend_security_policies": "BackendSecurityPolicy",
    "backend_traffic_policies":  "BackendTrafficPolicy",
    "service_entries":           "ServiceEntry",
    "envoy_backends":            "Backend",
    "argocd_applications":       "Application",
    "argocd_appprojects":        "AppProject",
}


def _make_edge(from_id: str, to_id: str, reason: str, rule: str,
               source: str = "rule") -> dict:
    return {"from_id": from_id, "to_id": to_id, "reason": reason,
            "rule": rule, "source": source}


def _ensure_node(nodes: list, seen: set, node_id: str, kind: str,
                 namespace: str, name: str, labels: dict | None = None) -> None:
    if node_id not in seen:
        seen.add(node_id)
        nodes.append(_make_node(node_id, kind, namespace, name, labels))


def _ensure_virtual(nodes: list, seen: set, name: str,
                    display_name: str | None = None) -> str:
    nid = _virtual(name)
    if nid not in seen:
        seen.add(nid)
        nodes.append(_make_node(nid, "Virtual", "", display_name or name))
    return nid


def _ensure_aws(nodes: list, seen: set, service: str) -> str:
    nid = _aws(service)
    if nid not in seen:
        seen.add(nid)
        nodes.append(_make_node(nid, "AWS", "", service))
    return nid


# ── Rules ─────────────────────────────────────────────────────────────────────

def _apply_ns01(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """NS-01: Deployments in istio-injection=enabled namespaces depend on istiod."""
    injected = {
        ns["metadata"]["name"]
        for ns in inventory["namespaces"]
        if ns["metadata"].get("labels", {}).get("istio-injection") == "enabled"
    }
    if not injected:
        logger.info("[analyzer] NS-01: no istio-injection=enabled namespaces found")
        return

    istiod_id = _ensure_virtual(nodes, seen, "istiod")
    count = 0
    for d in inventory["deployments"]:
        ns = d["metadata"]["namespace"]
        name = d["metadata"]["name"]
        if ns in injected:
            dep_id = _node_id("Deployment", ns, name)
            _ensure_node(nodes, seen, dep_id, "Deployment", ns, name,
                         d["metadata"].get("labels"))
            edges.append(_make_edge(dep_id, istiod_id,
                                    f"Deployment in istio-injection=enabled namespace '{ns}'",
                                    "NS-01"))
            count += 1
    logger.info("[analyzer] NS-01: %d edges added", count)


def _apply_as01_as02(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """AS-01: HPA → target Deployment. AS-02: target Deployment → metrics-server."""
    ms_id = None
    count_01 = count_02 = 0
    for hpa in inventory["hpas"]:
        ns = hpa["metadata"]["namespace"]
        name = hpa["metadata"]["name"]
        ref = hpa.get("spec", {}).get("scaleTargetRef", {})
        target_kind = ref.get("kind", "Deployment")
        target_name = ref.get("name", "")
        if not target_name:
            continue

        hpa_id = _node_id("HorizontalPodAutoscaler", ns, name)
        _ensure_node(nodes, seen, hpa_id, "HorizontalPodAutoscaler", ns, name,
                     hpa["metadata"].get("labels"))

        if target_kind == "Deployment":
            deploy_id = _node_id("Deployment", ns, target_name)
            _ensure_node(nodes, seen, deploy_id, "Deployment", ns, target_name)
            # AS-01
            edges.append(_make_edge(hpa_id, deploy_id,
                                    f"HPA scales Deployment '{target_name}'", "AS-01"))
            count_01 += 1
            # AS-02
            if ms_id is None:
                ms_id = _ensure_virtual(nodes, seen, "metrics-server")
            edges.append(_make_edge(deploy_id, ms_id,
                                    "Deployment targeted by HPA requires metrics-server",
                                    "AS-02"))
            count_02 += 1

    logger.info("[analyzer] AS-01: %d edges, AS-02: %d edges", count_01, count_02)


def _apply_eg01(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """EG-01: AIGatewayRoute parentRefs → Gateway."""
    count = 0
    for route in inventory["ai_gateway_routes"]:
        ns = route["metadata"]["namespace"]
        name = route["metadata"]["name"]
        route_id = _node_id("AIGatewayRoute", ns, name)
        _ensure_node(nodes, seen, route_id, "AIGatewayRoute", ns, name,
                     route["metadata"].get("labels"))

        for ref in route.get("spec", {}).get("parentRefs", []):
            gw_ns = ref.get("namespace", ns)
            gw_name = ref.get("name", "")
            if not gw_name:
                continue
            gw_id = _node_id("Gateway", gw_ns, gw_name)
            _ensure_node(nodes, seen, gw_id, "Gateway", gw_ns, gw_name)
            edges.append(_make_edge(route_id, gw_id,
                                    f"AIGatewayRoute parentRef → Gateway '{gw_name}'",
                                    "EG-01"))
            count += 1
    logger.info("[analyzer] EG-01: %d edges", count)


def _apply_eg02(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """EG-02: AIGatewayRoute backendRefs → AIServiceBackend."""
    count = 0
    for route in inventory["ai_gateway_routes"]:
        ns = route["metadata"]["namespace"]
        name = route["metadata"]["name"]
        route_id = _node_id("AIGatewayRoute", ns, name)
        _ensure_node(nodes, seen, route_id, "AIGatewayRoute", ns, name,
                     route["metadata"].get("labels"))

        for rule in route.get("spec", {}).get("rules", []):
            for bref in rule.get("backendRefs", []):
                backend_ns = bref.get("namespace", ns)
                backend_name = bref.get("name", "")
                if not backend_name:
                    continue
                backend_id = _node_id("AIServiceBackend", backend_ns, backend_name)
                _ensure_node(nodes, seen, backend_id, "AIServiceBackend",
                             backend_ns, backend_name)
                edges.append(_make_edge(route_id, backend_id,
                                        f"AIGatewayRoute backendRef → AIServiceBackend '{backend_name}'",
                                        "EG-02"))
                count += 1
    logger.info("[analyzer] EG-02: %d edges", count)


def _apply_eg03(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """EG-03: AIServiceBackend → Envoy Backend CRD (gateway.envoyproxy.io/v1alpha1)."""
    # Build a lookup: backend name → backend item
    backends_by_name: dict[str, dict] = {}
    for b in inventory.get("envoy_backends", []):
        bname = b["metadata"]["name"]
        bns = b["metadata"]["namespace"]
        backends_by_name[(bns, bname)] = b
        backends_by_name[("", bname)] = b  # fallback without namespace

    count = 0
    for asb in inventory["ai_service_backends"]:
        ns = asb["metadata"]["namespace"]
        name = asb["metadata"]["name"]
        asb_id = _node_id("AIServiceBackend", ns, name)
        _ensure_node(nodes, seen, asb_id, "AIServiceBackend", ns, name,
                     asb["metadata"].get("labels"))

        # AIServiceBackend spec.backendRef points to the Backend CRD
        bref = asb.get("spec", {}).get("backendRef", {})
        bname = bref.get("name", "")
        bns = bref.get("namespace", ns)
        if not bname:
            continue

        backend = backends_by_name.get((bns, bname)) or backends_by_name.get(("", bname))
        if backend:
            b_id = _node_id("Backend", backend["metadata"]["namespace"],
                            backend["metadata"]["name"])
            _ensure_node(nodes, seen, b_id, "Backend",
                         backend["metadata"]["namespace"],
                         backend["metadata"]["name"],
                         backend["metadata"].get("labels"))
        else:
            # Backend CRD not found in inventory (may not be installed); create placeholder
            b_id = _node_id("Backend", bns, bname)
            _ensure_node(nodes, seen, b_id, "Backend", bns, bname)

        edges.append(_make_edge(asb_id, b_id,
                                f"AIServiceBackend backendRef → Backend '{bname}'",
                                "EG-03"))
        count += 1
    logger.info("[analyzer] EG-03: %d edges", count)


def _apply_eg04(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """EG-04: BackendSecurityPolicy targetRefs → AIServiceBackend."""
    count = 0
    for bsp in inventory["backend_security_policies"]:
        ns = bsp["metadata"]["namespace"]
        name = bsp["metadata"]["name"]
        bsp_id = _node_id("BackendSecurityPolicy", ns, name)
        _ensure_node(nodes, seen, bsp_id, "BackendSecurityPolicy", ns, name,
                     bsp["metadata"].get("labels"))

        for ref in bsp.get("spec", {}).get("targetRefs", []):
            target_ns = ref.get("namespace", ns)
            target_name = ref.get("name", "")
            target_kind = ref.get("kind", "AIServiceBackend")
            if not target_name:
                continue
            target_id = _node_id(target_kind, target_ns, target_name)
            _ensure_node(nodes, seen, target_id, target_kind, target_ns, target_name)
            edges.append(_make_edge(bsp_id, target_id,
                                    f"BackendSecurityPolicy targetRef → {target_kind} '{target_name}'",
                                    "EG-04"))
            count += 1
    logger.info("[analyzer] EG-04: %d edges", count)


def _apply_eg05(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """EG-05: BackendSecurityPolicy credentialsRef (Secret) → Secret node."""
    count = 0
    for bsp in inventory["backend_security_policies"]:
        ns = bsp["metadata"]["namespace"]
        name = bsp["metadata"]["name"]
        bsp_id = _node_id("BackendSecurityPolicy", ns, name)
        _ensure_node(nodes, seen, bsp_id, "BackendSecurityPolicy", ns, name,
                     bsp["metadata"].get("labels"))

        spec = bsp.get("spec", {})
        # AWS credentials: spec.aws.credentials.secretRef or spec.credentialsRef
        secret_name = (
            spec.get("aws", {}).get("credentials", {}).get("secretRef", {}).get("name")
            or spec.get("credentialsRef", {}).get("name")
        )
        if not secret_name:
            continue

        secret_ns = (
            spec.get("aws", {}).get("credentials", {}).get("secretRef", {}).get("namespace")
            or ns
        )
        secret_id = _node_id("Secret", secret_ns, secret_name)
        _ensure_node(nodes, seen, secret_id, "Secret", secret_ns, secret_name)
        edges.append(_make_edge(bsp_id, secret_id,
                                f"BackendSecurityPolicy AWS credentials → Secret '{secret_name}'",
                                "EG-05"))
        count += 1
    logger.info("[analyzer] EG-05: %d edges", count)


def _apply_se_aws(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """SE-AWS: ServiceEntry *.amazonaws.com hosts → aws/* virtual nodes.
    Deployments in the same namespace as the ServiceEntry also depend on it.
    """
    # Build deployment lookup by namespace
    deploys_by_ns: dict[str, list[dict]] = {}
    for d in inventory["deployments"]:
        deploys_by_ns.setdefault(d["metadata"]["namespace"], []).append(d)

    count_se = count_deploy = 0
    for se in inventory["service_entries"]:
        ns = se["metadata"]["namespace"]
        name = se["metadata"]["name"]
        hosts = se.get("spec", {}).get("hosts", [])

        aws_services = set()
        for host in hosts:
            svc = _extract_aws_service(host)
            if svc:
                aws_services.add(svc)

        if not aws_services:
            continue

        se_id = _node_id("ServiceEntry", ns, name)
        _ensure_node(nodes, seen, se_id, "ServiceEntry", ns, name,
                     se["metadata"].get("labels"))

        for svc in aws_services:
            aws_id = _ensure_aws(nodes, seen, svc)
            edges.append(_make_edge(se_id, aws_id,
                                    f"ServiceEntry host matches aws/{svc}",
                                    "SE-AWS"))
            count_se += 1

        # Deployments in same namespace depend on this ServiceEntry
        for d in deploys_by_ns.get(ns, []):
            dep_id = _node_id("Deployment", ns, d["metadata"]["name"])
            _ensure_node(nodes, seen, dep_id, "Deployment", ns,
                         d["metadata"]["name"], d["metadata"].get("labels"))
            edges.append(_make_edge(dep_id, se_id,
                                    f"Deployment in namespace '{ns}' can use ServiceEntry '{name}'",
                                    "SE-AWS"))
            count_deploy += 1

    logger.info("[analyzer] SE-AWS: %d SE->aws edges, %d deploy->SE edges",
                count_se, count_deploy)


def _apply_cd01_cd02(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """CD-01: Application → AppProject. CD-02: Deployments in destination ns → Application."""
    # Build AppProject lookup: name → id (they live in argocd namespace)
    appprojects_by_name: dict[str, str] = {}
    for proj in inventory.get("argocd_appprojects", []):
        pns   = proj["metadata"]["namespace"]
        pname = proj["metadata"]["name"]
        pid   = _node_id("AppProject", pns, pname)
        _ensure_node(nodes, seen, pid, "AppProject", pns, pname,
                     proj["metadata"].get("labels"))
        appprojects_by_name[pname] = pid

    # Build deployment lookup: namespace → list[Deployment node ids]
    deploys_by_ns: dict[str, list[tuple[str, dict]]] = {}
    for d in inventory.get("deployments", []):
        dns = d["metadata"]["namespace"]
        deploys_by_ns.setdefault(dns, []).append((d["metadata"]["name"], d))

    count_01 = count_02 = 0
    for app in inventory.get("argocd_applications", []):
        ns   = app["metadata"]["namespace"]
        name = app["metadata"]["name"]
        app_id = _node_id("Application", ns, name)
        _ensure_node(nodes, seen, app_id, "Application", ns, name,
                     app["metadata"].get("labels"))

        # CD-01: Application → AppProject
        project_name = app.get("spec", {}).get("project", "")
        if project_name and project_name in appprojects_by_name:
            edges.append(_make_edge(app_id, appprojects_by_name[project_name],
                                    f"Application '{name}' uses AppProject '{project_name}'",
                                    "CD-01"))
            count_01 += 1

        # CD-02: Deployments in destination namespace → Application
        dest_ns = app.get("spec", {}).get("destination", {}).get("namespace", "")
        if dest_ns:
            for dep_name, dep in deploys_by_ns.get(dest_ns, []):
                dep_id = _node_id("Deployment", dest_ns, dep_name)
                _ensure_node(nodes, seen, dep_id, "Deployment", dest_ns, dep_name,
                             dep["metadata"].get("labels"))
                edges.append(_make_edge(dep_id, app_id,
                                        f"Deployment '{dep_name}' in '{dest_ns}' managed by Application '{name}'",
                                        "CD-02"))
                count_02 += 1

    logger.info("[analyzer] CD-01: %d edges, CD-02: %d edges", count_01, count_02)


def _apply_rl01(inventory: dict, nodes: list, seen: set, edges: list) -> None:
    """RL-01: BackendTrafficPolicy with rateLimit → targets + virtual/redis."""
    redis_id = None
    count = 0
    for btp in inventory["backend_traffic_policies"]:
        ns = btp["metadata"]["namespace"]
        name = btp["metadata"]["name"]
        spec = btp.get("spec", {})

        if not spec.get("rateLimit"):
            continue

        btp_id = _node_id("BackendTrafficPolicy", ns, name)
        _ensure_node(nodes, seen, btp_id, "BackendTrafficPolicy", ns, name,
                     btp["metadata"].get("labels"))

        if redis_id is None:
            redis_id = _ensure_virtual(nodes, seen, "redis")

        edges.append(_make_edge(btp_id, redis_id,
                                "Rate limiting BackendTrafficPolicy requires Redis",
                                "RL-01"))
        count += 1

        for ref in spec.get("targetRefs", []):
            target_kind = ref.get("kind", "Gateway")
            target_ns = ref.get("namespace", ns)
            target_name = ref.get("name", "")
            if not target_name:
                continue
            target_id = _node_id(target_kind, target_ns, target_name)
            _ensure_node(nodes, seen, target_id, target_kind, target_ns, target_name)
            edges.append(_make_edge(btp_id, target_id,
                                    f"BackendTrafficPolicy targets {target_kind} '{target_name}'",
                                    "RL-01"))
            count += 1

    logger.info("[analyzer] RL-01: %d edges", count)


# ── LLM-assisted fallback ─────────────────────────────────────────────────────

def _build_spec_map(inventory: dict) -> dict[str, dict]:
    """Return node_id → raw resource dict for every CRD item in the inventory."""
    spec_map: dict[str, dict] = {}
    for inv_key, kind in _CRD_INVENTORY_KEYS.items():
        for item in inventory.get(inv_key, []):
            ns   = item["metadata"]["namespace"]
            name = item["metadata"]["name"]
            spec_map[_node_id(kind, ns, name)] = item
    return spec_map


_LLM_SYSTEM = (
    "You are a Kubernetes dependency analyzer. "
    "Given a resource's metadata and spec, identify what other Kubernetes components it depends on. "
    "Respond ONLY with a valid JSON array and no other text."
)

_LLM_USER_TEMPLATE = """\
Resource:
  Kind:      {kind}
  Namespace: {namespace}
  Name:      {name}
  Labels:    {labels}
{spec_block}
Known cluster resources (use exact IDs from this list when possible):
{known_nodes}

You may also reference external dependencies using:
  aws/<service>   — AWS services inferred from FQDNs (e.g. aws/bedrock-runtime, aws/s3)
  virtual/<name>  — infrastructure components (e.g. virtual/istiod, virtual/metrics-server)

Return a JSON array where each object has:
  "depends_on" : exact resource ID from the list above, or aws/* / virtual/* format
  "reason"     : one-sentence explanation

Return [] if no dependencies can be determined.
"""


def _llm_infer_deps(
    node_id: str,
    node: dict,
    resource_data: dict | None,
    known_node_ids: list[str],
    llm: ChatOpenAI,
) -> list[dict]:
    """Call Claude via Envoy AI Gateway; returns list of {depends_on, reason}."""
    spec_block = ""
    if resource_data:
        spec = resource_data.get("spec", {})
        try:
            spec_yaml = yaml.dump(spec, default_flow_style=False)
            # Truncate very large specs to keep prompt size sane
            if len(spec_yaml) > 2000:
                spec_yaml = spec_yaml[:2000] + "\n  # ... (truncated)"
            spec_block = f"Spec:\n{spec_yaml}\n"
        except Exception:
            spec_block = f"Spec: {str(spec)[:500]}\n"

    prompt = _LLM_USER_TEMPLATE.format(
        kind=node["kind"],
        namespace=node["namespace"] or "cluster-scoped",
        name=node["name"],
        labels=json.dumps(node.get("labels", {})),
        spec_block=spec_block,
        known_nodes="\n".join(known_node_ids),
    )

    try:
        response = llm.invoke([
            SystemMessage(content=_LLM_SYSTEM),
            HumanMessage(content=prompt),
        ])
        text = response.content.strip()
        # Extract the JSON array even if the model wraps it in prose
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start == -1 or end == 0:
            logger.warning("[analyzer] LLM returned no JSON array for %s", node_id)
            return []
        return json.loads(text[start:end])
    except Exception as exc:
        logger.warning("[analyzer] LLM call failed for %s: %s", node_id, exc)
        return []


def _gateway_reachable(gateway_url: str) -> bool:
    """Quick TCP connectivity check — avoids firing 8 retrying LLM calls if gateway is down."""
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(gateway_url)
        host   = parsed.hostname or "localhost"
        port   = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _apply_llm_fallback(
    inventory: dict,
    nodes: list,
    seen: set,
    edges: list,
    llm: ChatOpenAI,
) -> None:
    """LLM-01: for nodes with no detected outbound edges, ask Claude for dependencies."""
    spec_map      = _build_spec_map(inventory)
    has_outbound  = {e["from_id"] for e in edges}
    known_node_ids = [n["id"] for n in nodes]

    candidates = [
        n for n in nodes
        if n["id"] not in has_outbound
        and n["kind"] not in {"Virtual", "AWS"}
        and (n["id"] in spec_map or n["kind"] not in _RULE_HANDLED_KINDS)
    ]

    print(f"\n[analyzer] LLM-01: {len(candidates)} nodes sent to Claude for inference")
    count = 0
    for node in candidates:
        node_id    = node["id"]
        inferred   = _llm_infer_deps(node_id, node, spec_map.get(node_id),
                                     known_node_ids, llm)
        for dep in inferred:
            target_id = dep.get("depends_on", "").strip()
            reason    = dep.get("reason", "LLM-inferred dependency")
            if not target_id:
                continue

            # Ensure target node exists (create virtual/aws nodes on the fly)
            if target_id not in seen:
                parts = target_id.split("/")
                if parts[0] == "virtual" and len(parts) == 2:
                    _ensure_virtual(nodes, seen, parts[1])
                elif parts[0] == "aws" and len(parts) == 2:
                    _ensure_aws(nodes, seen, parts[1])
                elif len(parts) == 3:
                    _ensure_node(nodes, seen, target_id, parts[0], parts[1], parts[2])
                else:
                    logger.warning("[analyzer] LLM returned invalid node ID %r — skipping",
                                   target_id)
                    continue

            edges.append(_make_edge(node_id, target_id, reason, "LLM-01", source="llm"))
            logger.info("[analyzer] LLM-01: %s → %s", node_id, target_id)
            count += 1

    logger.info("[analyzer] LLM-01: %d inferred edges total", count)


# ── Main node ─────────────────────────────────────────────────────────────────

def analyze_node(state: dict) -> dict:
    """LangGraph node: apply dependency rules then LLM fallback; return nodes + edges."""
    inventory   = state.get("inventory", {})
    use_llm     = state.get("use_llm", True)
    gateway_url = state.get("gateway_url", "")
    model_id    = state.get("model_id", "")

    print("\n" + "=" * 60)
    print("  ANALYZER")
    print("=" * 60)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    # Seed all Gateways as nodes (they appear as targets before any route rule fires)
    for gw in inventory.get("gateways", []):
        ns   = gw["metadata"]["namespace"]
        name = gw["metadata"]["name"]
        gw_id = _node_id("Gateway", ns, name)
        _ensure_node(nodes, seen_ids, gw_id, "Gateway", ns, name,
                     gw["metadata"].get("labels"))

    _apply_ns01(inventory, nodes, seen_ids, edges)
    _apply_as01_as02(inventory, nodes, seen_ids, edges)
    _apply_eg01(inventory, nodes, seen_ids, edges)
    _apply_eg02(inventory, nodes, seen_ids, edges)
    _apply_eg03(inventory, nodes, seen_ids, edges)
    _apply_eg04(inventory, nodes, seen_ids, edges)
    _apply_eg05(inventory, nodes, seen_ids, edges)
    _apply_se_aws(inventory, nodes, seen_ids, edges)
    _apply_rl01(inventory, nodes, seen_ids, edges)
    _apply_cd01_cd02(inventory, nodes, seen_ids, edges)

    # ── LLM fallback ──────────────────────────────────────────────────────────
    if use_llm and gateway_url and model_id:
        llm = ChatOpenAI(
            model=model_id,
            base_url=f"{gateway_url}/v1",
            api_key="not-needed",
            timeout=30,
            max_retries=0,          # fail fast; pipeline continues without LLM
            # Envoy AI Gateway routes by this header, not the body model field
            default_headers={"x-ai-eg-model": model_id},
        )
        if _gateway_reachable(gateway_url):
            _apply_llm_fallback(inventory, nodes, seen_ids, edges, llm)
        else:
            print(f"\n[analyzer] LLM-01: skipped — gateway unreachable at {gateway_url}")
            print("  Tip: kubectl port-forward -n envoy-gateway-system "
                  "svc/envoy-default-envoy-ai-gateway-basic-21a9f8f8 8080:80")
    elif use_llm:
        print("\n[analyzer] LLM-01: skipped — GATEWAY_URL or MODEL_ID not set")

    # Deduplicate edges (same from+to+rule)
    seen_edges: set[tuple] = set()
    dedup_edges = []
    for e in edges:
        key = (e["from_id"], e["to_id"], e["rule"])
        if key not in seen_edges:
            seen_edges.add(key)
            dedup_edges.append(e)

    rule_edges = sum(1 for e in dedup_edges if e.get("source") == "rule")
    llm_edges  = sum(1 for e in dedup_edges if e.get("source") == "llm")
    print(f"\n[analyzer] Nodes: {len(nodes)}  Edges: {len(dedup_edges)} "
          f"(rule={rule_edges}, llm={llm_edges})")
    print(f"[analyzer] Rules fired: "
          f"{', '.join(sorted({e['rule'] for e in dedup_edges})) or 'none'}")

    return {"nodes": nodes, "edges": dedup_edges}
