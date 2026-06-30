"""
Graph Scorer — LangGraph node that builds a NetworkX DiGraph from the analyzer's
nodes and edges, then computes a criticality score for each node.

Edge direction: FROM dependent TO dependency.
  e.g. Deployment/default/agent → virtual/istiod

In this orientation:
  - virtual/istiod has HIGH in-degree (many things point to it)
  - Standard nx.pagerank(G) naturally gives it a high score — nodes pointed to
    by many other nodes rank highly. No reversal is needed.

Final criticality_score ∈ [0, 100]:
  score = (0.6 * pagerank_normalized + 0.4 * in_degree_normalized) * 100
"""

import logging

import networkx as nx

logger = logging.getLogger(__name__)


def _normalize(d: dict[str, float]) -> dict[str, float]:
    """Min-max normalize values to [0, 1]. Returns new dict."""
    if not d:
        return {}
    lo = min(d.values())
    hi = max(d.values())
    span = hi - lo
    if span == 0:
        return {k: 0.0 for k in d}
    return {k: (v - lo) / span for k, v in d.items()}


def score_node(state: dict) -> dict:
    """LangGraph node: compute criticality scores for all graph nodes."""
    nodes: list[dict] = state.get("nodes", [])
    edges: list[dict] = state.get("edges", [])

    print("\n" + "=" * 60)
    print("  SCORER")
    print("=" * 60)
    print(f"[scorer] Building graph: {len(nodes)} nodes, {len(edges)} edges")

    G = nx.DiGraph()

    for n in nodes:
        G.add_node(n["id"], kind=n["kind"], namespace=n["namespace"],
                   name=n["name"], labels=n["labels"])

    for e in edges:
        # Ensure both endpoints exist (edges referencing nodes not in the
        # inventory — e.g. placeholder Backend nodes — are still valid)
        if e["from_id"] not in G:
            G.add_node(e["from_id"], kind="Unknown", namespace="", name=e["from_id"], labels={})
        if e["to_id"] not in G:
            G.add_node(e["to_id"], kind="Unknown", namespace="", name=e["to_id"], labels={})
        G.add_edge(e["from_id"], e["to_id"], reason=e["reason"], rule=e["rule"])

    if G.number_of_nodes() == 0:
        logger.warning("[scorer] Empty graph — no nodes to score")
        return {"scores": {}}

    # PageRank: on G (dependent→dependency), high-dependency nodes score high
    try:
        pr = nx.pagerank(G, alpha=0.85, max_iter=200)
    except nx.PowerIterationFailedConvergence:
        logger.warning("[scorer] PageRank did not converge — using uniform scores")
        n_nodes = G.number_of_nodes()
        pr = {node: 1.0 / n_nodes for node in G.nodes}

    idc = nx.in_degree_centrality(G)

    pr_norm = _normalize(pr)
    idc_norm = _normalize(idc)

    scores: dict[str, dict] = {}
    for node_id in G.nodes:
        raw_score = 0.6 * pr_norm.get(node_id, 0.0) + 0.4 * idc_norm.get(node_id, 0.0)
        scores[node_id] = {
            "criticality_score": round(raw_score * 100, 2),
            "pagerank": pr.get(node_id, 0.0),
            "in_degree_centrality": idc.get(node_id, 0.0),
            "in_degree_raw": G.in_degree(node_id),
            "out_degree_raw": G.out_degree(node_id),
            # predecessors = nodes that depend ON this node (useful for blast-radius)
            "dependents": list(G.predecessors(node_id)),
            # successors = nodes this node depends on
            "dependencies": list(G.successors(node_id)),
        }

    # Print top-10 for quick verification
    top10 = sorted(scores.items(), key=lambda x: x[1]["criticality_score"], reverse=True)[:10]
    print("\n[scorer] Top-10 by criticality:")
    for rank, (nid, s) in enumerate(top10, 1):
        kind = G.nodes[nid].get("kind", "?")
        print(f"  {rank:2d}.  {s['criticality_score']:6.1f}  [{kind:25s}]  {nid}")

    return {"scores": scores}
