"""
OPA Policy Violation Fix Pipeline
──────────────────────────────────
Multi-agent LangGraph pipeline with retry loop (max 3 fix attempts):

  START → [Scanner] → [should_fix_or_report?] → [Fix] ──┐
                              ↓                           │
                      [Generate Report]  ←── (rescan) ───┘
                              ↓
                             END

  Scanner        — runs conftest against manifests and Helm charts, emits violations
  Fix            — calls LLM to fix each violating file (max 3 attempts)
  Generate Report — compares initial vs. final violations, writes README.md

Checkpointing:
  Uses AsyncSqliteSaver so the graph can resume after a crash.
  Each invocation gets a unique thread_id; pass --thread-id to resume a specific run.
"""

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from fix_agent import _llm, _invoke_with_retry_async, fix_node
from scanner_agent import scanner_node

BASE_DIR   = os.getenv("BASE_DIR", "/app")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", str(Path(BASE_DIR) / "fixed"))

_README_SYSTEM = """\
You are a technical writer producing a README.md for a folder of auto-fixed Kubernetes manifests.
Use GitHub-flavored markdown. Be concise, accurate, and structured.
Do not use emojis. Do not include code blocks unless showing a YAML snippet as an example.
"""


class PipelineState(TypedDict):
    base_dir: str
    output_dir: str
    scan_dir: str                    # starts as base_dir; switches to output_dir after first fix
    violations: list[dict]
    fixed_files: list[str]
    readme_content: str
    fix_summary: list[dict]
    fix_attempt: int                 # incremented by fix_node before returning
    initial_violations: list[dict]   # snapshot from the first scan only
    all_fix_summaries: list[list[dict]]  # accumulated per-attempt fix summaries


# ── Routing ───────────────────────────────────────────────────────────────────

def should_fix_or_report(state: PipelineState) -> str:
    if not state.get("violations"):
        return "generate_report"
    if state.get("fix_attempt", 0) >= 3:
        return "generate_report"
    return "fix"


# ── Report node ───────────────────────────────────────────────────────────────

async def generate_report(state: PipelineState) -> dict:
    initial     = state.get("initial_violations", [])
    final       = state.get("violations", [])
    fix_attempt = state.get("fix_attempt", 0)
    output_dir  = Path(state.get("output_dir", OUTPUT_DIR))

    print("\n" + "=" * 55)
    print("  GENERATE REPORT")
    print("=" * 55)
    print(f"  Initial violations : {len(initial)}")
    print(f"  Final violations   : {len(final)}")
    print(f"  Fix attempts made  : {fix_attempt}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if not initial:
        readme = "# OPA Fix Report\n\nNo policy violations detected on initial scan.\n"
        (output_dir / "README.md").write_text(readme)
        print("\n[report] No initial violations — wrote static README.md")
        return {"readme_content": readme}

    final_keys = {(v["file"], v["message"]) for v in final}
    fixed_violations   = [v for v in initial if (v["file"], v["message"]) not in final_keys]
    unfixed_violations = [v for v in initial if (v["file"], v["message"]) in final_keys]

    print(f"\n[report] Fixed: {len(fixed_violations)}  Still failing: {len(unfixed_violations)}")

    readme_prompt = (
        f"Generate a README.md for a folder of auto-fixed Kubernetes manifests.\n\n"
        f"Fix attempts made: {fix_attempt} (maximum allowed: 3)\n\n"
        f"FIXED violations (JSON):\n{json.dumps(fixed_violations, indent=2)}\n\n"
        f"STILL-FAILING violations (JSON):\n{json.dumps(unfixed_violations, indent=2)}\n\n"
        "The README must contain:\n"
        "1. A one-paragraph summary.\n"
        f"2. A table with columns: File | Violation | Status | Attempts Made\n"
        f"   Status values: 'Fixed' or 'Still Failing after {fix_attempt} attempt(s)'\n"
        "3. A 'Policies Enforced' section describing the four rules:\n"
        "   - resource-limits, image-tag, required-labels, security-context\n"
        "4. A short note explaining that for Helm charts, value-driven violations "
        "(image tag) are fixed in values.yaml while structural violations are fixed in the template.\n"
    )

    llm = _llm()
    response = await _invoke_with_retry_async(llm, [
        SystemMessage(content=_README_SYSTEM),
        HumanMessage(content=readme_prompt),
    ])
    readme = response.content.strip()

    readme_path = output_dir / "README.md"
    readme_path.write_text(readme + "\n")

    print(f"\n[report] README.md written → {readme_path}")
    print("\n── README.md ─────────────────────────────────────────")
    print(readme)
    print("──────────────────────────────────────────────────────")

    return {"readme_content": readme}


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_pipeline(checkpointer=None):
    graph = StateGraph(PipelineState)
    graph.add_node("scan",            scanner_node)
    graph.add_node("fix",             fix_node)
    graph.add_node("generate_report", generate_report)

    graph.add_edge(START, "scan")
    graph.add_conditional_edges("scan", should_fix_or_report, {
        "fix":             "fix",
        "generate_report": "generate_report",
    })
    graph.add_edge("fix", "scan")
    graph.add_edge("generate_report", END)

    return graph.compile(checkpointer=checkpointer)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="OPA Policy Violation Fix Pipeline")
    parser.add_argument(
        "--thread-id", default=None,
        help="Resume a specific run by its thread ID (printed on first run)",
    )
    args = parser.parse_args()

    thread_id    = args.thread_id or str(uuid.uuid4())
    checkpoint_db = Path(OUTPUT_DIR) / ".checkpoint.db"

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    print("=" * 55)
    print("  OPA POLICY VIOLATION FIX PIPELINE")
    print("=" * 55)
    print(f"  Base dir      : {BASE_DIR}")
    print(f"  Output dir    : {OUTPUT_DIR}")
    print(f"  Thread ID     : {thread_id}")
    print(f"  Checkpoint DB : {checkpoint_db}")

    thread_config = {"configurable": {"thread_id": thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        pipeline = build_pipeline(checkpointer)

        final_state = await pipeline.ainvoke(
            {
                "base_dir":           BASE_DIR,
                "output_dir":         OUTPUT_DIR,
                "scan_dir":           BASE_DIR,
                "violations":         [],
                "fixed_files":        [],
                "readme_content":     "",
                "fix_summary":        [],
                "fix_attempt":        0,
                "initial_violations": [],
                "all_fix_summaries":  [],
            },
            config=thread_config,
        )

    print("\n" + "=" * 55)
    print("  PIPELINE COMPLETE")
    print("=" * 55)
    initial_count = len(final_state.get("initial_violations", []))
    final_count   = len(final_state.get("violations", []))
    fixed_count   = initial_count - final_count
    print(f"  Initial violations : {initial_count}")
    print(f"  Fixed              : {fixed_count}")
    print(f"  Still failing      : {final_count}")
    print(f"  Fix attempts made  : {final_state.get('fix_attempt', 0)}")
    print(f"  Files fixed        : {len(final_state.get('fixed_files', []))}")

    if final_state.get("fixed_files"):
        print("\n  Fixed files:")
        for f in final_state["fixed_files"]:
            try:
                rel = Path(f).relative_to(OUTPUT_DIR)
            except ValueError:
                rel = Path(f).name
            print(f"    - {rel}")

    if not final_state.get("initial_violations"):
        print("\n  No violations detected — all manifests are compliant.")
    elif final_count == 0:
        print("\n  All violations fixed successfully.")
    else:
        print(f"\n  WARNING: {final_count} violation(s) could not be fixed after "
              f"{final_state.get('fix_attempt', 0)} attempt(s). See README.md for details.")


if __name__ == "__main__":
    asyncio.run(main())
