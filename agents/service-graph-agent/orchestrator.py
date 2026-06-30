"""
Service Dependency Graph Agent — LangGraph pipeline

Pipeline: START -> collect -> analyze -> score -> report -> END

Rule-based detection + LLM-assisted fallback (Llama 3 via Envoy AI Gateway).
Produces:
  /output/dependency-graph.json    machine-readable graph + scores + component map
  /output/service-graph.html       interactive pyvis visualization (resource level)
  /output/component-graph.html     interactive pyvis visualization (component level)
  /output/criticality-report.txt   ranked criticality table

Usage:
  python orchestrator.py              # in-cluster (K8s Job, uses ServiceAccount token)
  python orchestrator.py --local      # local dev (uses ~/.kube/config)
  python orchestrator.py --no-llm     # skip LLM fallback (rule-based only)
  python orchestrator.py --fresh      # ignore existing checkpoint, start from scratch

Environment variables:
  OUTPUT_DIR   Directory to write output files (default: /output)
  GATEWAY_URL  Envoy AI Gateway base URL for LLM calls
               (default: in-cluster envoy-ai-gateway-basic service URL)
  MODEL_ID     Model header sent to the AI Gateway
               (default: us.meta.llama3-3-70b-instruct-v1:0)

Fault tolerance:
  After each node completes, LangGraph writes the full GraphState to a SQLite
  checkpoint DB at <output_dir>/.checkpoint.db (on the hostPath volume).  If the
  pod is killed mid-run and the K8s Job retries, the new pod finds the checkpoint,
  skips already-completed nodes, and resumes from the first pending node.

  A checkpoint is reused only if it is < 30 minutes old (covers the Job's retry
  window).  Older checkpoints are deleted and the pipeline starts fresh.  Use
  --fresh to force a clean start regardless of checkpoint age.

Local LLM access requires the gateway to be port-forwarded:
  kubectl port-forward -n envoy-gateway-system \\
    svc/envoy-default-envoy-ai-gateway-basic-21a9f8f8 8080:80
  GATEWAY_URL=http://localhost:8080 python orchestrator.py --local
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from analyzer import analyze_node
from collector import collect_node
from reporter import report_node
from scorer import score_node

OUTPUT_DIR  = os.getenv("OUTPUT_DIR", "/output")
GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local",
)
MODEL_ID = os.getenv("MODEL_ID", "us.meta.llama3-3-70b-instruct-v1:0")

# A checkpoint younger than this is treated as "resumable" (same Job retry window).
# Beyond 30 min the checkpoint is stale (different Job run) and gets cleared.
_CHECKPOINT_RESUME_WINDOW_SECS = 30 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


class GraphState(TypedDict):
    local_mode:  bool
    output_dir:  str
    gateway_url: str      # Envoy AI Gateway base URL
    model_id:    str      # x-ai-eg-model header value
    use_llm:     bool     # False when --no-llm flag is set
    inventory:   dict     # resource_type -> list[dict]
    nodes:       list     # {id, kind, namespace, name, labels}
    edges:       list     # {from_id, to_id, reason, rule, source}
    scores:      dict     # node_id -> {criticality_score, ...}
    output_paths: dict    # {json, html, component_html, txt}


def build_pipeline(checkpointer=None):
    graph = StateGraph(GraphState)
    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("score", score_node)
    graph.add_node("report", report_node)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "score")
    graph.add_edge("score", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)


def _clear_stale_checkpoint(checkpoint_db: Path, force: bool) -> None:
    """Delete checkpoint if --fresh or if it is older than the resume window."""
    if not checkpoint_db.exists():
        return
    age = time.time() - checkpoint_db.stat().st_mtime
    if force:
        checkpoint_db.unlink()
        print("  Checkpoint : cleared (--fresh)")
    elif age > _CHECKPOINT_RESUME_WINDOW_SECS:
        checkpoint_db.unlink()
        print(f"  Checkpoint : cleared (stale — {int(age // 60)}m old, limit 30m)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kubernetes Service Dependency Graph Agent")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Load ~/.kube/config instead of in-cluster ServiceAccount token",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM-assisted analysis (rule-based only, no gateway required)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any existing checkpoint and start the pipeline from scratch",
    )
    args = parser.parse_args()

    output_dir = OUTPUT_DIR
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    use_llm = not args.no_llm

    print("=" * 60)
    print("  SERVICE DEPENDENCY GRAPH AGENT")
    print("=" * 60)
    print(f"  Mode       : {'local (~/.kube/config)' if args.local else 'in-cluster'}")
    print(f"  Output dir : {output_dir}")
    print(f"  LLM        : {'enabled — ' + MODEL_ID if use_llm else 'disabled (--no-llm)'}")
    if use_llm:
        print(f"  Gateway    : {GATEWAY_URL}")

    checkpoint_db = Path(output_dir) / ".checkpoint.db"
    _clear_stale_checkpoint(checkpoint_db, force=args.fresh)

    initial_state: GraphState = {
        "local_mode":  args.local,
        "output_dir":  output_dir,
        "gateway_url": GATEWAY_URL,
        "model_id":    MODEL_ID,
        "use_llm":     use_llm,
        "inventory":   {},
        "nodes":       [],
        "edges":       [],
        "scores":      {},
        "output_paths": {},
    }

    thread_config = {"configurable": {"thread_id": "service-graph-run"}}

    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        pipeline = build_pipeline(checkpointer)

        existing = pipeline.get_state(thread_config)

        if existing.values and existing.next:
            # Mid-run crash: checkpoint has partial state, resume from next pending node.
            completed = [
                n for n in ("collect", "analyze", "score", "report")
                if n not in existing.next
            ]
            print(f"  Checkpoint : RESUMING  (done={completed}, next={list(existing.next)})")
            # Pass empty update — LangGraph uses checkpointed state for all fields.
            final_state = pipeline.invoke({}, config=thread_config)
        else:
            if existing.values and not existing.next:
                # Previous run completed fully but checkpoint wasn't cleared (e.g. pod
                # restarted after a successful exit before the Job controller marked it
                # Complete).  Start a fresh run so output files are regenerated.
                print("  Checkpoint : previous run complete — starting fresh run")
            else:
                print("  Checkpoint : fresh start")
            final_state = pipeline.invoke(initial_state, config=thread_config)

    print("\n" + "=" * 60)
    print("  PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Nodes found   : {len(final_state['nodes'])}")
    print(f"  Edges found   : {len(final_state['edges'])}")
    print(f"  Output files  :")
    for key, path in final_state.get("output_paths", {}).items():
        print(f"    [{key}] {path}")


if __name__ == "__main__":
    main()
