"""
Report Generator — LangGraph node that writes four output artifacts:
  1. dependency-graph.json   — machine-readable graph with scores + component section
  2. service-graph.html      — interactive pyvis visualization at resource level
  3. component-graph.html    — interactive pyvis visualization at component level
  4. criticality-report.txt  — human-readable ranked table

Node colors in the resource HTML visualization (by kind):
  Deployment              #4CAF50  green
  Virtual                 #FF9800  orange
  AWS                     #FF5722  deep-orange
  Gateway / AIGatewayRoute #2196F3 blue
  AIServiceBackend        #00BCD4  cyan
  BackendSecurityPolicy   #9C27B0  purple
  BackendTrafficPolicy    #673AB7  deep-purple
  HorizontalPodAutoscaler #E91E63  pink
  Secret                  #F44336  red
  ServiceEntry            #009688  teal
  Backend                 #795548  brown
  default                 #607D8B  blue-grey
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Resource-level colors ────────────────────────────────────────────────────

_KIND_COLORS: dict[str, str] = {
    "Deployment":              "#4CAF50",
    "Virtual":                 "#FF9800",
    "AWS":                     "#FF5722",
    "Gateway":                 "#2196F3",
    "AIGatewayRoute":          "#2196F3",
    "AIServiceBackend":        "#00BCD4",
    "BackendSecurityPolicy":   "#9C27B0",
    "BackendTrafficPolicy":    "#673AB7",
    "HorizontalPodAutoscaler": "#E91E63",
    "Secret":                  "#F44336",
    "ServiceEntry":            "#009688",
    "Backend":                 "#795548",
    "Application":             "#F06292",  # pink — ArgoCD Application
    "AppProject":              "#AD1457",  # dark-pink — ArgoCD AppProject
}
_DEFAULT_RESOURCE_COLOR = "#607D8B"

# ── Component-level mapping ──────────────────────────────────────────────────

_NS_COMPONENT_MAP: dict[str, str] = {
    "istio-system":          "Istio",
    "envoy-gateway-system":  "Envoy Gateway Core",
    "redis-system":          "Redis",
    "kubernetes-mcp-server": "MCP Server",
    "service-graph-agent":   "Service Graph Agent",
    "kube-system":           "Kubernetes System",
    "argocd":                "ArgoCD",
}

_VIRTUAL_COMPONENT_MAP: dict[str, str] = {
    "istiod":         "Istio",
    "metrics-server": "Kubernetes System",
    "redis":          "Redis",
}

# Strip these suffixes (iteratively, in any combination) to derive a component
# stem from a default-namespace resource name that doesn't match any Gateway prefix.
_NAME_SUFFIXES_TO_STRIP: tuple[str, ...] = (
    "-aws-bedrock-anthropic",
    "-aws-bedrock",
    "-credentials",
    "-testupstream",
    "-openai",
    "-aws",
)

_COMPONENT_COLORS: dict[str, str] = {
    "Istio":               "#009688",  # teal
    "Redis":               "#FF9800",  # orange
    "Kubernetes System":   "#607D8B",  # blue-grey
    "Envoy Gateway Core":  "#3F51B5",  # indigo
    "MCP Server":          "#8BC34A",  # light-green
    "Service Graph Agent": "#795548",  # brown
    "ArgoCD":              "#E91E63",  # pink
}
_DEFAULT_COMPONENT_COLOR = "#2196F3"  # blue for gateway-type components
_AWS_COMPONENT_COLOR     = "#FF5722"  # deep-orange for AWS


# ── Component helpers ────────────────────────────────────────────────────────

def _gateway_label(gw_name: str) -> str:
    """'envoy-ai-gateway-basic' -> 'Envoy AI Gateway [Basic]'"""
    prefix = "envoy-ai-gateway-"
    if gw_name.startswith(prefix):
        variant = gw_name[len(prefix):]
        return f"Envoy AI Gateway [{variant.title()}]"
    return " ".join(p.title() for p in gw_name.split("-"))


def _strip_name_suffixes(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES_TO_STRIP:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                changed = True
                break
    return name


def _component_of(node_id: str, gw_names_sorted: list[str]) -> str:
    """Map a node ID to a human-readable component label."""
    parts = node_id.split("/")
    kind  = parts[0]

    if kind == "virtual":
        vname = parts[1] if len(parts) > 1 else ""
        return _VIRTUAL_COMPONENT_MAP.get(vname, f"Virtual: {vname}")

    if kind == "aws":
        service = parts[1] if len(parts) > 1 else "unknown"
        return f"AWS: {service.replace('-', ' ').title()}"

    ns   = parts[1] if len(parts) > 1 else ""
    name = parts[2] if len(parts) > 2 else ""

    if ns in _NS_COMPONENT_MAP:
        return _NS_COMPONENT_MAP[ns]

    if ns == "default":
        # Match longest gateway prefix first
        for gw_name in gw_names_sorted:
            if name == gw_name or name.startswith(gw_name + "-"):
                return _gateway_label(gw_name)
        # Derive component from name by stripping known suffixes
        stem = _strip_name_suffixes(name)
        return " ".join(p.title() for p in stem.split("-"))

    return " ".join(p.title() for p in ns.split("-"))


def _build_component_graph(
    nodes: list[dict],
    edges: list[dict],
    scores: dict,
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Collapse resource nodes into components; return (comp_nodes, comp_edges, node_to_comp)."""
    gw_names_sorted: list[str] = sorted(
        (n["name"] for n in nodes if n["kind"] == "Gateway"),
        key=len,
        reverse=True,
    )

    node_to_comp: dict[str, str] = {
        n["id"]: _component_of(n["id"], gw_names_sorted) for n in nodes
    }

    comp_scores:  dict[str, float]    = {}
    comp_members: dict[str, int]      = {}
    comp_kinds:   dict[str, set[str]] = {}
    for n in nodes:
        comp  = node_to_comp[n["id"]]
        score = scores.get(n["id"], {}).get("criticality_score", 0.0)
        comp_scores[comp]  = max(comp_scores.get(comp, 0.0), score)
        comp_members[comp] = comp_members.get(comp, 0) + 1
        comp_kinds.setdefault(comp, set()).add(n["kind"])

    comp_nodes = [
        {
            "id":                comp,
            "label":             comp,
            "criticality_score": comp_scores[comp],
            "member_count":      comp_members[comp],
            "kinds":             sorted(comp_kinds[comp]),
        }
        for comp in sorted(comp_scores)
    ]

    seen_edges: set[tuple[str, str, str]] = set()
    comp_edges: list[dict] = []
    for e in edges:
        src = node_to_comp.get(e["from_id"], "")
        dst = node_to_comp.get(e["to_id"],   "")
        if src and dst and src != dst:
            key = (src, dst, e["rule"])
            if key not in seen_edges:
                seen_edges.add(key)
                comp_edges.append({
                    "from_comp": src,
                    "to_comp":   dst,
                    "rule":      e["rule"],
                    "reason":    e["reason"],
                })

    return comp_nodes, comp_edges, node_to_comp


# ── Output writers ───────────────────────────────────────────────────────────

def _short_label(node_id: str) -> str:
    """'Deployment/default/my-app' -> 'my-app'"""
    return node_id.split("/")[-1]


def _write_json(output_dir: Path, nodes: list[dict], edges: list[dict],
                scores: dict) -> Path:
    out_nodes = []
    for n in nodes:
        s = scores.get(n["id"], {})
        out_nodes.append({
            **n,
            "criticality_score":  s.get("criticality_score", 0.0),
            "in_degree":          s.get("in_degree_raw", 0),
            "pagerank":           round(s.get("pagerank", 0.0), 6),
            "dependents_count":   len(s.get("dependents", [])),
            "dependencies_count": len(s.get("dependencies", [])),
        })
    out_nodes.sort(key=lambda x: x["criticality_score"], reverse=True)

    out_edges = [
        {"from": e["from_id"], "to": e["to_id"], "reason": e["reason"],
         "rule": e["rule"], "source": e.get("source", "rule")}
        for e in edges
    ]

    comp_nodes, comp_edges, node_to_comp = _build_component_graph(nodes, edges, scores)

    payload = {
        "nodes": out_nodes,
        "edges": out_edges,
        "components": {
            "nodes": comp_nodes,
            "edges": [
                {"from": ce["from_comp"], "to": ce["to_comp"],
                 "rule": ce["rule"], "reason": ce["reason"]}
                for ce in comp_edges
            ],
            "node_to_component": node_to_comp,
        },
        "summary": {
            "total_nodes":           len(out_nodes),
            "total_edges":           len(out_edges),
            "total_components":      len(comp_nodes),
            "total_component_edges": len(comp_edges),
            "generated_at":          datetime.now(timezone.utc).isoformat(),
        },
    }

    path = output_dir / "dependency-graph.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("[reporter] Written %s (%d nodes, %d edges, %d components)",
                path, len(out_nodes), len(out_edges), len(comp_nodes))
    return path


def _write_resource_html(output_dir: Path, nodes: list[dict], edges: list[dict],
                         scores: dict) -> Path:
    """Resource-level interactive graph (service-graph.html)."""
    try:
        from pyvis.network import Network
    except ImportError:
        logger.warning("[reporter] pyvis not installed — skipping resource HTML")
        path = output_dir / "service-graph.html"
        path.write_text("<html><body><p>pyvis not installed</p></body></html>",
                        encoding="utf-8")
        return path

    net = Network(
        height="900px",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="#e0e0e0",
        cdn_resources="in_line",
        directed=True,
    )

    for n in nodes:
        s     = scores.get(n["id"], {})
        score = s.get("criticality_score", 0.0)
        color = _KIND_COLORS.get(n["kind"], _DEFAULT_RESOURCE_COLOR)
        size  = max(10, 10 + score * 0.5)
        label = _short_label(n["id"])
        title = (
            f"<b>{n['kind']}</b>: {n['name']}<br>"
            f"Namespace: {n['namespace'] or '-'}<br>"
            f"Criticality: <b>{score:.1f}</b><br>"
            f"Dependents: {len(s.get('dependents', []))}<br>"
            f"Dependencies: {len(s.get('dependencies', []))}"
        )
        net.add_node(n["id"], label=label, title=title, color=color, size=size,
                     font={"size": 11})

    for e in edges:
        net.add_edge(e["from_id"], e["to_id"],
                     title=e["reason"], label=e["rule"],
                     color="#aaaaaa",
                     font={"size": 9, "color": "#cccccc"})

    net.set_options("""
    {
      "physics": {
        "enabled": true,
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {
          "gravitationalConstant": -80,
          "centralGravity": 0.01,
          "springLength": 120,
          "springConstant": 0.08,
          "avoidOverlap": 0.3
        },
        "stabilization": { "iterations": 200 }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true,
        "keyboard": true
      },
      "edges": {
        "smooth": { "type": "dynamic" },
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.6 } }
      }
    }
    """)

    path = output_dir / "service-graph.html"
    path.write_text(net.generate_html(), encoding="utf-8")
    logger.info("[reporter] Written %s (%d resource nodes)", path, len(nodes))
    return path


def _write_component_html(output_dir: Path, nodes: list[dict], edges: list[dict],
                          scores: dict) -> Path:
    """Component-level interactive graph (component-graph.html)."""
    try:
        from pyvis.network import Network
    except ImportError:
        path = output_dir / "component-graph.html"
        path.write_text("<html><body><p>pyvis not installed</p></body></html>",
                        encoding="utf-8")
        return path

    comp_nodes, comp_edges, _ = _build_component_graph(nodes, edges, scores)

    net = Network(
        height="900px",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="#e0e0e0",
        cdn_resources="in_line",
        directed=True,
    )

    for cn in comp_nodes:
        comp  = cn["id"]
        score = cn["criticality_score"]
        if comp.startswith("AWS:"):
            color = _AWS_COMPONENT_COLOR
        else:
            color = _COMPONENT_COLORS.get(comp, _DEFAULT_COMPONENT_COLOR)
        size  = max(25, 25 + score * 0.6)
        title = (
            f"<b>{comp}</b><br>"
            f"Member resources: {cn['member_count']}<br>"
            f"Kinds: {', '.join(cn['kinds'])}<br>"
            f"Max criticality: <b>{score:.1f}</b>"
        )
        net.add_node(comp, label=comp, title=title, color=color, size=size,
                     font={"size": 14})

    for ce in comp_edges:
        net.add_edge(ce["from_comp"], ce["to_comp"],
                     title=ce["reason"], label=ce["rule"],
                     color="#aaaaaa",
                     font={"size": 10, "color": "#cccccc"})

    net.set_options("""
    {
      "physics": {
        "enabled": true,
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {
          "gravitationalConstant": -120,
          "centralGravity": 0.02,
          "springLength": 200,
          "springConstant": 0.05,
          "avoidOverlap": 0.8
        },
        "stabilization": { "iterations": 150 }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true,
        "keyboard": true
      },
      "edges": {
        "smooth": { "type": "dynamic" },
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.8 } }
      }
    }
    """)

    path = output_dir / "component-graph.html"
    path.write_text(net.generate_html(), encoding="utf-8")
    logger.info("[reporter] Written %s (%d components, %d cross-component edges)",
                path, len(comp_nodes), len(comp_edges))
    return path


def _write_report(output_dir: Path, nodes: list[dict], scores: dict) -> Path:
    ranked = sorted(nodes, key=lambda n: scores.get(n["id"], {}).get("criticality_score", 0),
                    reverse=True)

    lines = [
        "SERVICE DEPENDENCY GRAPH - CRITICALITY REPORT",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 80,
        f"{'Rank':>4}  {'Score':>7}  {'Kind':<25}  {'Dependents':>10}  Node ID",
        "-" * 80,
    ]
    for rank, n in enumerate(ranked, 1):
        s          = scores.get(n["id"], {})
        score      = s.get("criticality_score", 0.0)
        dependents = len(s.get("dependents", []))
        lines.append(
            f"{rank:>4}  {score:>7.1f}  {n['kind']:<25}  {dependents:>10}  {n['id']}"
        )

    lines += [
        "",
        "=" * 80,
        "DEPENDENCY DETAIL (blast radius - what depends on each component)",
        "=" * 80,
    ]
    for n in ranked[:20]:
        s    = scores.get(n["id"], {})
        deps = s.get("dependents", [])
        if deps:
            lines.append(f"\n{n['id']}  (score={s.get('criticality_score',0):.1f})")
            for d in deps[:10]:
                lines.append(f"  <- {d}")
            if len(deps) > 10:
                lines.append(f"  ... and {len(deps) - 10} more")

    content = "\n".join(lines) + "\n"
    path = output_dir / "criticality-report.txt"
    path.write_text(content, encoding="utf-8")
    logger.info("[reporter] Written %s", path)
    return path


# ── LangGraph node ───────────────────────────────────────────────────────────

def report_node(state: dict) -> dict:
    """LangGraph node: generate all four output artifacts."""
    nodes:      list[dict] = state.get("nodes", [])
    edges:      list[dict] = state.get("edges", [])
    scores:     dict       = state.get("scores", {})
    output_dir: Path       = Path(state.get("output_dir", "/output"))

    print("\n" + "=" * 60)
    print("  REPORTER")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    json_path      = _write_json(output_dir, nodes, edges, scores)
    html_path      = _write_resource_html(output_dir, nodes, edges, scores)
    comp_html_path = _write_component_html(output_dir, nodes, edges, scores)
    txt_path       = _write_report(output_dir, nodes, scores)

    print(f"\n[reporter] Output files:")
    print(f"  JSON          : {json_path}")
    print(f"  Resource HTML : {html_path}")
    print(f"  Component HTML: {comp_html_path}")
    print(f"  TXT           : {txt_path}")

    return {
        "output_paths": {
            "json":           str(json_path),
            "html":           str(html_path),
            "component_html": str(comp_html_path),
            "txt":            str(txt_path),
        }
    }
