"""
Fix Agent — LangGraph node that reads policy violations, uses an LLM (via Envoy AI
Gateway) to produce corrected YAML, and writes fixed files to an output directory.

Two fix strategies:
  manifest — single-file fix: read the file, fix it, write it back.
  helm     — dual-file fix:  read both the template AND values.yaml, let the LLM
             decide which file owns each violation, write both separately.
             Value-driven violations (image tag) → values.yaml
             Structural violations (resources, securityContext, labels) → template

README generation has moved to the generate_report node in main.py.
"""

import asyncio
import os
import shutil
from collections import defaultdict
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import RateLimitError

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local",
)
MODEL_ID = os.getenv("MODEL_ID", "us.meta.llama3-3-70b-instruct-v1:0")

# ── Prompts ────────────────────────────────────────────────────────────────────

_MANIFEST_FIX_SYSTEM = """\
You are a Kubernetes YAML expert. Fix ALL listed policy violations in the provided manifest.

STRICT RULES:
1. Return ONLY valid YAML — no markdown fences, no explanations, no extra text.
2. Preserve every existing field that does not violate policy.
3. Fixes to apply:
   - Missing resources: add under each container:
       resources:
         requests:
           cpu: "100m"
           memory: "128Mi"
         limits:
           cpu: "200m"
           memory: "256Mi"
   - Resource CPU or memory limit exceeds 2x the corresponding request: reduce the limit to
     exactly 2x the request. Example: request "100m" → limit "200m", request "128Mi" → limit "256Mi".
     Keep requests unchanged.
   - Image tag ':latest' or absent: pin to a specific version (e.g. busybox:1.36.1, nginx:1.25.3).
   - Missing pod template labels 'app'/'env': add to spec.template.metadata.labels
     using the value from metadata.name for 'app' and 'dev' for 'env'.
   - Missing securityContext: add under each container:
       securityContext:
         runAsNonRoot: true
         runAsUser: 1000
         allowPrivilegeEscalation: false
   - Missing topologySpreadConstraints on a Deployment: add to spec.template.spec:
       topologySpreadConstraints:
         - maxSkew: 1
           topologyKey: kubernetes.io/hostname
           whenUnsatisfiable: ScheduleAnyway
           labelSelector:
             matchLabels:
               app: <use the value of metadata.name>
   - PodDisruptionBudget with spec.minAvailable less than 1: set spec.minAvailable to 1.
"""

_HELM_FIX_SYSTEM = """\
You are a Kubernetes and Helm expert. Fix ALL listed policy violations in this Helm chart.

CRITICAL RULE — which file owns each fix:
  • Value-driven violations → fix ONLY in values.yaml. Leave template {{ .Values.* }} references unchanged.
    This includes:
    - Image tag ':latest' or missing → pin to a specific version in values.yaml
    - Resource limit values that exceed 2x their corresponding request values → reduce the limit
      values in values.yaml so each limit is at most 2x the corresponding request.
      Example: if requests.cpu is "100m" and limits.cpu is "500m", change limits.cpu to "200m".
    - pdb.minAvailable is less than 1 → set pdb.minAvailable to 1 in values.yaml.
  • Structural violations → fix ONLY in the template file. Leave values.yaml unchanged for these.
    This includes:
    - Missing resources block (no requests/limits at all) → add hardcoded resources to the template.
    - Missing securityContext → add to each container in the template.
    - Missing labels 'app'/'env' → add to spec.template.metadata.labels in the template.
    - Missing topologySpreadConstraints → add to spec.template.spec in the template.

OUTPUT FORMAT — return exactly these two sections, nothing else:
---VALUES.YAML---
<complete fixed values.yaml content>
---TEMPLATE---
<complete fixed template content>

Additional fix rules:
  - Resources to add per container in the template (only when entirely missing):
      resources:
        requests:
          cpu: "100m"
          memory: "128Mi"
        limits:
          cpu: "200m"
          memory: "256Mi"
  - Labels to add to spec.template.metadata.labels in the template:
      app: {{ .Release.Name }}
      env: dev
  - securityContext to add per container in the template:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        allowPrivilegeEscalation: false
  - topologySpreadConstraints to add to spec.template.spec in the template (Deployment only):
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: {{ .Release.Name }}
  - Pinned image tag for nginx: 1.25.3 (set in values.yaml as: tag: "1.25.3")
  - Both sections are REQUIRED even when one file has no changes.
  - Do not add markdown fences inside either section.
"""

# ── LLM helpers ───────────────────────────────────────────────────────────────

def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=MODEL_ID,
        base_url=f"{GATEWAY_URL}/v1",
        api_key="not-needed",
        timeout=120,
    )


async def _invoke_with_retry_async(llm: ChatOpenAI, messages: list, max_retries: int = 6) -> object:
    """Invoke the LLM asynchronously, retrying on 429 with exponential backoff."""
    delay = 10
    for attempt in range(max_retries):
        try:
            return await llm.ainvoke(messages)
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            print(f"      [rate-limit] 429 — retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end = len(lines) - 1 if lines and lines[-1].strip() == "```" else len(lines)
        return "\n".join(lines[start:end]).strip()
    return text


def _parse_helm_response(text: str) -> tuple[str, str]:
    """Parse the ---VALUES.YAML--- / ---TEMPLATE--- delimited LLM response."""
    VALUES_MARKER = "---VALUES.YAML---"
    TEMPLATE_MARKER = "---TEMPLATE---"

    text = text.strip()
    if VALUES_MARKER not in text or TEMPLATE_MARKER not in text:
        raise ValueError(
            f"LLM response missing required section markers.\n"
            f"Expected '---VALUES.YAML---' and '---TEMPLATE---'.\n"
            f"Got:\n{text[:400]}"
        )

    v_start = text.index(VALUES_MARKER) + len(VALUES_MARKER)
    t_start = text.index(TEMPLATE_MARKER)
    t_content_start = t_start + len(TEMPLATE_MARKER)

    values_content = _strip_fences(text[v_start:t_start])
    template_content = _strip_fences(text[t_content_start:])
    return values_content, template_content


# ── Fix strategies ────────────────────────────────────────────────────────────

async def _fix_manifest(src: Path, messages: list[str], scan_dir: Path,
                        output_dir: Path, llm: ChatOpenAI) -> dict:
    """Fix a plain Kubernetes manifest file."""
    violations_text = "\n".join(f"- {m}" for m in messages)
    response = await _invoke_with_retry_async(llm, [
        SystemMessage(content=_MANIFEST_FIX_SYSTEM),
        HumanMessage(content=f"VIOLATIONS TO FIX:\n{violations_text}\n\nORIGINAL YAML:\n{src.read_text()}"),
    ])
    fixed_yaml = _strip_fences(response.content)

    try:
        rel = src.relative_to(scan_dir)
    except ValueError:
        rel = Path(src.name)

    out_path = output_dir / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(fixed_yaml + "\n")

    print(f"\n      ✓ Written → {out_path}")
    _print_yaml(fixed_yaml)
    return {"file": str(rel), "violations": messages, "fixed": True}


async def _fix_helm_template(src: Path, values_file: Path, messages: list[str],
                             scan_dir: Path, output_dir: Path, llm: ChatOpenAI,
                             meta: dict) -> list[dict]:
    """
    Fix a Helm chart template + values.yaml pair.
    Returns fix summary entries for both files written.
    """
    if not values_file.exists():
        print(f"      [helm] WARNING: values.yaml not found at {values_file} — falling back to manifest fix")
        return [await _fix_manifest(src, messages, scan_dir, output_dir, llm)]

    violations_text = "\n".join(f"- {m}" for m in messages)
    prompt = (
        f"VIOLATIONS (detected in the rendered chart output):\n{violations_text}\n\n"
        f"VALUES.YAML:\n{values_file.read_text()}\n\n"
        f"TEMPLATE FILE ({src.name}):\n{src.read_text()}"
    )

    response = await _invoke_with_retry_async(llm, [
        SystemMessage(content=_HELM_FIX_SYSTEM),
        HumanMessage(content=prompt),
    ])

    fixed_values, fixed_template = _parse_helm_response(response.content)

    summary = []

    # Write fixed values.yaml
    try:
        values_rel = values_file.relative_to(scan_dir)
    except ValueError:
        values_rel = Path(values_file.name)
    values_out = output_dir / values_rel
    values_out.parent.mkdir(parents=True, exist_ok=True)
    values_out.write_text(fixed_values + "\n")
    print(f"\n      ✓ values.yaml → {values_out}")
    _print_yaml(fixed_values)
    summary.append({"file": str(values_rel), "violations": ["image-tag (value-driven)"], "fixed": True})

    # Ensure Chart.yaml is present in the output chart dir (required by helm template on retry)
    src_chart_yaml = Path(meta["chart_dir"]) / "Chart.yaml"
    out_chart_yaml = values_out.parent / "Chart.yaml"
    if src_chart_yaml.exists() and not out_chart_yaml.exists():
        shutil.copy2(src_chart_yaml, out_chart_yaml)

    # Write fixed template
    try:
        tpl_rel = src.relative_to(scan_dir)
    except ValueError:
        tpl_rel = Path(src.name)
    tpl_out = output_dir / tpl_rel
    tpl_out.parent.mkdir(parents=True, exist_ok=True)
    tpl_out.write_text(fixed_template + "\n")
    print(f"\n      ✓ template → {tpl_out}")
    _print_yaml(fixed_template)
    structural = [m for m in messages if "image tag" not in m and "image has no tag" not in m]
    summary.append({"file": str(tpl_rel), "violations": structural or messages, "fixed": True})

    return summary


def _print_yaml(content: str) -> None:
    print("      ── YAML ────────────────────────────────────────")
    for line in content.splitlines():
        print(f"      {line}")
    print("      ────────────────────────────────────────────────")


# ── Main node ─────────────────────────────────────────────────────────────────

async def fix_node(state: dict) -> dict:
    violations: list[dict] = state.get("violations", [])
    base_dir    = Path(state.get("base_dir", "/app"))
    scan_dir    = Path(state.get("scan_dir", str(base_dir)))
    output_dir  = Path(state.get("output_dir", str(base_dir / "fixed")))
    current_attempt = state.get("fix_attempt", 0)
    all_summaries   = list(state.get("all_fix_summaries", []))

    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 55)
    print("  FIX AGENT")
    print("=" * 55)
    print(f"  Model      : {MODEL_ID}")
    print(f"  Gateway    : {GATEWAY_URL}")
    print(f"  Scan dir   : {scan_dir}")
    print(f"  Output dir : {output_dir}")
    print(f"  Attempt    : {current_attempt + 1}/3")
    print(f"  Violations : {len(violations)}")

    if not violations:
        print("\n[fix] No violations to fix — nothing to do.")
        all_summaries.append([])
        return {
            "fixed_files":       [],
            "fix_summary":       [],
            "fix_attempt":       current_attempt + 1,
            "scan_dir":          str(output_dir),
            "all_fix_summaries": all_summaries,
            "readme_content":    "",
        }

    llm = _llm()

    # Group by template file, preserving metadata from first matching violation
    by_file: dict[str, dict] = {}
    for v in violations:
        key = v["file"]
        if key not in by_file:
            by_file[key] = {"messages": [], "meta": v}
        by_file[key]["messages"].append(v["message"])

    fixed_files: list[str] = []
    fix_summary: list[dict] = []

    for source_file, group in by_file.items():
        src = Path(source_file)
        messages = group["messages"]
        meta = group["meta"]

        if not src.exists():
            print(f"\n[fix] WARNING: {source_file} not found — skipping")
            fix_summary.append({"file": source_file, "violations": messages,
                                 "fixed": False, "reason": "source file not found"})
            continue

        print(f"\n[fix] Processing: {src.name}  (source_type={meta['source_type']})")
        for m in messages:
            print(f"      ✗ {m}")

        if meta["source_type"] == "helm":
            entries = await _fix_helm_template(
                src=src,
                values_file=Path(meta["values_file"]),
                messages=messages,
                scan_dir=scan_dir,
                output_dir=output_dir,
                llm=llm,
                meta=meta,
            )
            for e in entries:
                fix_summary.append(e)
                fixed_files.append(str(output_dir / e["file"]))
        else:
            entry = await _fix_manifest(src, messages, scan_dir, output_dir, llm)
            fix_summary.append(entry)
            fixed_files.append(str(output_dir / entry["file"]))

    all_summaries.append(fix_summary)

    return {
        "fixed_files":       fixed_files,
        "fix_summary":       fix_summary,
        "fix_attempt":       current_attempt + 1,
        "scan_dir":          str(output_dir),
        "all_fix_summaries": all_summaries,
        "readme_content":    "",
    }
