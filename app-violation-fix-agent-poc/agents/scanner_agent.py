"""
Scanner Agent — LangGraph node that runs conftest against manifests and Helm charts.
No LLM is used here; this is deterministic policy evaluation.

Violation dict schema:
  - file        : absolute path to the source file that needs fixing
  - message     : human-readable violation description from conftest
  - source_type : "manifest" | "helm"
  - chart_dir   : (helm only) absolute path to chart root (contains values.yaml)
  - values_file : (helm only) absolute path to values.yaml
"""

import asyncio
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(os.getenv("BASE_DIR", "/app"))


async def _run_conftest(paths: list[str], policy_dir: str) -> list[dict]:
    if not paths:
        return []
    proc = await asyncio.create_subprocess_exec(
        "conftest", "test",
        "-p", policy_dir,
        "--output", "json",
        "--no-color",
        *paths,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode()

    if not output.strip():
        if proc.returncode not in (0, 1):
            print(f"[scanner] conftest error (exit {proc.returncode}): {stderr.decode()[:300]}")
        return []

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        print(f"[scanner] Failed to parse conftest JSON:\n{output[:400]}")
        return []

    violations = []
    for entry in data:
        filename = entry.get("filename", "unknown")
        for failure in entry.get("failures", []):
            violations.append({
                "file": filename,
                "message": failure.get("msg", ""),
            })
    return violations


async def scanner_node(state: dict) -> dict:
    base_dir   = Path(state.get("base_dir", str(BASE_DIR)))
    scan_dir   = Path(state.get("scan_dir", str(base_dir)))
    fix_attempt = state.get("fix_attempt", 0)
    policy_dir = str(base_dir / "policies")   # always from original base_dir
    manifest_dir = scan_dir / "sample-manifests"
    chart_dir    = scan_dir / "sample-charts" / "my-app"

    print("\n" + "=" * 55)
    print("  SCANNER AGENT")
    print("=" * 55)
    print(f"  Base dir    : {base_dir}")
    print(f"  Scan dir    : {scan_dir}")
    print(f"  Policies    : {policy_dir}")
    print(f"  Manifests   : {manifest_dir}")
    print(f"  Helm chart  : {chart_dir}")
    print(f"  Attempt     : {fix_attempt}")

    all_violations: list[dict] = []

    # --- Scan plain manifest files (source_type: manifest) ---
    print("\n[scanner] Scanning sample-manifests/ ...")
    manifest_files = sorted(manifest_dir.glob("*.yaml")) if manifest_dir.exists() else []
    if manifest_files:
        raw = await _run_conftest([str(f) for f in manifest_files], policy_dir)
        viol = [{"source_type": "manifest", **v} for v in raw]
        print(f"[scanner] Found {len(viol)} violation(s) in manifests")
        all_violations.extend(viol)
    else:
        print("[scanner] No manifest files found")

    # --- Render Helm chart and scan (source_type: helm) ---
    print("\n[scanner] Rendering Helm chart with 'helm template' ...")
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = await asyncio.create_subprocess_exec(
            "helm", "template", "my-app", str(chart_dir), "--output-dir", tmpdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f"[scanner] helm template failed:\n{stderr.decode()[:400]}")
        else:
            rendered_tpl_dir = Path(tmpdir) / "my-app" / "templates"
            rendered_files = sorted(rendered_tpl_dir.glob("*.yaml"))
            raw = await _run_conftest([str(f) for f in rendered_files], policy_dir)

            viol = []
            for v in raw:
                rendered_name = Path(v["file"]).name
                viol.append({
                    "file": str(chart_dir / "templates" / rendered_name),
                    "message": v["message"],
                    "source_type": "helm",
                    "chart_dir": str(chart_dir),
                    "values_file": str(chart_dir / "values.yaml"),
                })
            print(f"[scanner] Found {len(viol)} violation(s) in Helm chart templates")
            all_violations.extend(viol)

    # --- Print grouped summary ---
    by_file: dict[str, list[str]] = defaultdict(list)
    for v in all_violations:
        by_file[v["file"]].append(v["message"])

    print(f"\n[scanner] ── Violation summary ({len(all_violations)} total) ──")
    for fpath, msgs in by_file.items():
        try:
            rel = Path(fpath).relative_to(scan_dir)
        except ValueError:
            rel = Path(fpath).name
        print(f"\n  {rel}")
        for m in msgs:
            print(f"    ✗ {m}")

    # --- Persist to violations.json alongside the scanned directory ---
    violations_json_path = scan_dir / "violations.json"
    violations_json_path.write_text(json.dumps(all_violations, indent=2))
    print(f"\n[scanner] violations.json written → {violations_json_path}")

    result: dict = {"violations": all_violations}
    if fix_attempt == 0:
        result["initial_violations"] = list(all_violations)
    return result
