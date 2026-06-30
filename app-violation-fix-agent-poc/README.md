# App Violation Fix Agent — POC

A LangGraph-based agent that scans Kubernetes manifests and Helm charts against OPA policies, automatically fixes violations using an LLM via Envoy AI Gateway, and produces a remediation report. Runs as a Kubernetes Job inside a Kind cluster.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Kubernetes Job (violation-fix-agent namespace)                      │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  LangGraph Pipeline  (AsyncSqliteSaver checkpoint per run)  │    │
│  │                                                               │    │
│  │   ┌──────────────┐                                           │    │
│  │   │  START        │                                           │    │
│  │   └──────┬───────┘                                           │    │
│  │          │                                                     │    │
│  │   ┌──────▼───────┐   conftest + helm template                │    │
│  │   │  scan node   │◄──────────────────────────┐               │    │
│  │   └──────┬───────┘   (re-scans fixed output) │               │    │
│  │          │                                     │               │    │
│  │    violations?──── no ──────────────┐         │               │    │
│  │    fix_attempt<3?                    │         │               │    │
│  │          │ yes                       │         │               │    │
│  │   ┌──────▼───────┐                  │         │               │    │
│  │   │  fix node    │──── LLM call ────┘─────────┘               │    │
│  │   │  (attempt    │   (up to 3 attempts, then generate_report)  │    │
│  │   │   1 / 2 / 3) │                                             │    │
│  │   └──────────────┘                                             │    │
│  │          │ (after max attempts or no violations)               │    │
│  │   ┌──────▼───────────┐                                        │    │
│  │   │ generate_report  │──── LLM call → README.md               │    │
│  │   └──────┬───────────┘                                        │    │
│  │          │                                                      │    │
│  │   ┌──────▼───────┐                                             │    │
│  │   │   END         │                                             │    │
│  │   └──────────────┘                                             │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
          │ LLM calls (OpenAI-compat /v1/chat/completions)
          ▼
┌─────────────────────────────────────────────────────┐
│  Envoy AI Gateway  (envoy-gateway-system namespace) │
│  routes by x-ai-eg-model header                     │
└──────────────────────────┬──────────────────────────┘
                           │
                    AWS Bedrock (Meta Llama 3.3 70B)
```

## How It Works

### 1 — Scan Node
Runs [conftest](https://www.conftest.dev/) with the OPA policies against every manifest file and every rendered Helm chart template. On retry passes it scans the **fixed output directory** (not the originals) so it can detect whether the previous fix actually resolved the violations.

### 2 — Fix Node (up to 3 attempts)
Groups violations by source file and calls the LLM once per file with the full policy-violation list and the original YAML. The LLM returns corrected YAML which is written to `OUTPUT_DIR`. For Helm charts it sends both the template and `values.yaml` together and returns a `---VALUES.YAML--- / ---TEMPLATE---` delimited response so value-driven fixes (image tag, resource limits) land in `values.yaml` and structural fixes (labels, securityContext, topologySpreadConstraints) land in the template.

After each fix the graph loops back to the scan node. The loop runs at most 3 times. The `scan_dir` state field switches from the original `BASE_DIR` to `OUTPUT_DIR` after the first fix so subsequent scans always verify the corrected files.

### 3 — Generate Report Node
Compares `initial_violations` (snapshot from first scan) against `violations` (from the final scan) to determine which violations were resolved and which remain. Calls the LLM to produce a `README.md` with a summary table:

| File | Violation | Status | Attempts Made |
|---|---|---|---|
| ... | ... | Fixed / Still Failing | 1–3 |

### Checkpointing
Each invocation gets a unique `thread_id` (UUID). `AsyncSqliteSaver` writes a checkpoint after every node, stored at `OUTPUT_DIR/.checkpoint.db`. Pass `--thread-id <id>` to resume an interrupted run from the last completed node without re-running earlier steps.

## OPA Policies

All policies live in `policies/` and are evaluated by conftest in `package main` with `deny[msg]` rules.

| Policy file | What it checks |
|---|---|
| `resource-limits.rego` | Every container in a Deployment or Job must declare `resources.limits.cpu` and `resources.limits.memory` |
| `image-tag.rego` | Container images must not use `:latest` or be untagged |
| `required-labels.rego` | Pod templates must carry `app` and `env` labels |
| `security-context.rego` | Every container must set `runAsNonRoot: true` and `allowPrivilegeEscalation: false` |
| `pdb-required.rego` | Any `PodDisruptionBudget` must have `spec.minAvailable >= 1` |
| `topology-spread.rego` | Deployments must define `spec.template.spec.topologySpreadConstraints` |
| `resource-ratio.rego` | CPU and memory limits must not exceed 2× their corresponding requests; handles `m` / `Mi` / `Gi` unit parsing |

## Directory Layout

```
app-violation-fix-agent-poc/
├── agents/
│   ├── main.py            # LangGraph graph, state, routing, generate_report, async entry point
│   ├── scanner_agent.py   # Async scan node — runs conftest and helm template subprocesses
│   ├── fix_agent.py       # Async fix node — LLM calls, manifest/Helm fix strategies
│   └── requirements.txt
├── policies/              # OPA Rego policy files (7 policies)
├── sample-manifests/      # Intentionally violating plain YAML files (job, deployment, pdb)
├── sample-charts/
│   └── my-app/            # Helm chart with intentional violations in values and templates
├── fixed/                 # Output from the last agent run (fixed YAML + README + violations.json)
└── Dockerfile             # Installs conftest, helm, Python deps; CMD runs agents/main.py
```

## Running Locally

```bash
# Install dependencies
pip install -r agents/requirements.txt
# conftest and helm must be on PATH

# Run (scans sample-manifests/ and sample-charts/, writes fixed output to poc-output/)
BASE_DIR=$(pwd) OUTPUT_DIR=$(pwd)/poc-output python agents/main.py

# Resume an interrupted run
python agents/main.py --thread-id <uuid-from-previous-run>
```

## Deploying to the Kind Cluster

```bash
# Build and push to the cluster's local registry
docker build -t localhost:5001/violation-fix-agent:latest app-violation-fix-agent-poc/
docker push localhost:5001/violation-fix-agent:latest

# Create namespace and run
kubectl create namespace violation-fix-agent
kubectl apply -f manifests/violation-fix-agent/job.yaml

# Watch output (fixed YAML is printed as it is written)
kubectl logs -n violation-fix-agent -l app=violation-fix-agent -f

# Retrieve fixed files from the Kind node's hostPath volume
docker exec ai-cluster-control-plane find /violation-fix-output -type f
```

Fixed files are written to `/output` inside the container, which maps to `/violation-fix-output` on the Kind node (`ai-cluster-control-plane`). Use `docker cp` to pull them locally:

```bash
docker cp ai-cluster-control-plane:/violation-fix-output/. app-violation-fix-agent-poc/fixed/
```

## LLM Integration

The agent calls the LLM through the Envoy AI Gateway using the OpenAI-compatible `/v1/chat/completions` endpoint. The model is selected by the `MODEL_ID` environment variable (default: `us.meta.llama3-3-70b-instruct-v1:0`). The gateway handles AWS credential injection, request routing, and rate limiting transparently.

Rate-limit errors (HTTP 429) are retried up to 6 times with exponential backoff (10 s → 20 s → 40 s, capped at 60 s).
