"""
Resource Collector — LangGraph node that fetches all required Kubernetes resources
from the cluster API. Supports both in-cluster (ServiceAccount token) and local
(~/.kube/config) modes. CRDs that are not installed return an empty list and log
a warning rather than raising an exception.

IMPORTANT: _list_secrets returns only metadata (name + namespace). Secret data is
never read or included in the inventory to prevent credential leakage.
"""

import logging
import sys

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


def _load_kube_config(local: bool) -> None:
    if local:
        config.load_kube_config()
        logger.info("[collector] Loaded kubeconfig from ~/.kube/config")
    else:
        config.load_incluster_config()
        logger.info("[collector] Loaded in-cluster ServiceAccount config")


def _list_namespaces(core: client.CoreV1Api) -> list[dict]:
    items = core.list_namespace().items or []
    result = []
    for ns in items:
        result.append({
            "metadata": {
                "name": ns.metadata.name,
                "labels": ns.metadata.labels or {},
                "annotations": ns.metadata.annotations or {},
            }
        })
    logger.info("[collector] Collected %d namespaces", len(result))
    return result


def _list_pods(core: client.CoreV1Api) -> list[dict]:
    items = core.list_pod_for_all_namespaces().items or []
    result = []
    for pod in items:
        owner_refs = []
        for ref in (pod.metadata.owner_references or []):
            owner_refs.append({"kind": ref.kind, "name": ref.name})
        result.append({
            "metadata": {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "labels": pod.metadata.labels or {},
                "annotations": pod.metadata.annotations or {},
                "ownerReferences": owner_refs,
            }
        })
    logger.info("[collector] Collected %d pods", len(result))
    return result


def _list_deployments(apps: client.AppsV1Api) -> list[dict]:
    items = apps.list_deployment_for_all_namespaces().items or []
    result = []
    for d in items:
        result.append({
            "metadata": {
                "name": d.metadata.name,
                "namespace": d.metadata.namespace,
                "labels": d.metadata.labels or {},
                "annotations": d.metadata.annotations or {},
            }
        })
    logger.info("[collector] Collected %d deployments", len(result))
    return result


def _list_services(core: client.CoreV1Api) -> list[dict]:
    items = core.list_service_for_all_namespaces().items or []
    result = []
    for svc in items:
        result.append({
            "metadata": {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "labels": svc.metadata.labels or {},
            },
            "spec": {
                "selector": svc.spec.selector or {} if svc.spec else {},
            },
        })
    logger.info("[collector] Collected %d services", len(result))
    return result


def _list_secrets(core: client.CoreV1Api) -> list[dict]:
    # Return ONLY name + namespace — never .data or .binaryData
    items = core.list_secret_for_all_namespaces().items or []
    result = []
    for s in items:
        result.append({
            "metadata": {
                "name": s.metadata.name,
                "namespace": s.metadata.namespace,
            }
        })
    logger.info("[collector] Collected %d secrets (names only)", len(result))
    return result


def _list_hpas(autoscaling: client.AutoscalingV2Api) -> list[dict]:
    items = autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces().items or []
    result = []
    for hpa in items:
        ref = hpa.spec.scale_target_ref if hpa.spec else None
        result.append({
            "metadata": {
                "name": hpa.metadata.name,
                "namespace": hpa.metadata.namespace,
                "labels": hpa.metadata.labels or {},
            },
            "spec": {
                "scaleTargetRef": {
                    "kind": ref.kind if ref else "Deployment",
                    "name": ref.name if ref else "",
                    "apiVersion": ref.api_version if ref else "apps/v1",
                } if ref else {}
            },
        })
    logger.info("[collector] Collected %d HPAs", len(result))
    return result


def _list_custom(
    crd: client.CustomObjectsApi,
    group: str,
    version: str,
    plural: str,
    label: str,
) -> list[dict]:
    """Fetch a CRD resource list. Returns [] with a warning if the CRD is not installed."""
    try:
        resp = crd.list_cluster_custom_object(group, version, plural)
        items = resp.get("items", [])
        logger.info("[collector] Collected %d %s", len(items), label)
        return items
    except ApiException as e:
        if e.status == 404:
            logger.warning("[collector] CRD not installed: %s — skipping", label)
            return []
        raise


def collect_node(state: dict) -> dict:
    """LangGraph node: collect all K8s resources into state['inventory']."""
    local_mode = state.get("local_mode", False)

    try:
        _load_kube_config(local_mode)
    except Exception as exc:
        logger.error("[collector] Failed to load kubeconfig: %s", exc)
        sys.exit(1)

    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    autoscaling = client.AutoscalingV2Api()
    crd = client.CustomObjectsApi()

    print("\n" + "=" * 60)
    print("  COLLECTOR")
    print("=" * 60)

    inventory = {
        "namespaces":               _list_namespaces(core),
        "pods":                     _list_pods(core),
        "deployments":              _list_deployments(apps),
        "services":                 _list_services(core),
        "secrets":                  _list_secrets(core),
        "hpas":                     _list_hpas(autoscaling),
        "gateways":                 _list_custom(crd, "gateway.networking.k8s.io", "v1", "gateways", "Gateway"),
        "ai_gateway_routes":        _list_custom(crd, "aigateway.envoyproxy.io", "v1beta1", "aigatewayroutes", "AIGatewayRoute"),
        "ai_service_backends":      _list_custom(crd, "aigateway.envoyproxy.io", "v1beta1", "aiservicebackends", "AIServiceBackend"),
        "backend_security_policies": _list_custom(crd, "aigateway.envoyproxy.io", "v1beta1", "backendsecuritypolicies", "BackendSecurityPolicy"),
        "backend_traffic_policies": _list_custom(crd, "aigateway.envoyproxy.io", "v1beta1", "backendtrafficpolicies", "BackendTrafficPolicy"),
        "service_entries":          _list_custom(crd, "networking.istio.io", "v1beta1", "serviceentries", "ServiceEntry"),
        "envoy_backends":           _list_custom(crd, "gateway.envoyproxy.io", "v1alpha1", "backends", "Backend"),
        "argocd_applications":      _list_custom(crd, "argoproj.io", "v1alpha1", "applications", "ArgoCD Application"),
        "argocd_appprojects":       _list_custom(crd, "argoproj.io", "v1alpha1", "appprojects", "ArgoCD AppProject"),
    }

    total = sum(len(v) for v in inventory.values())
    print(f"\n[collector] Total resources collected: {total}")
    for key, items in inventory.items():
        if items:
            print(f"  {key}: {len(items)}")

    return {"inventory": inventory}
