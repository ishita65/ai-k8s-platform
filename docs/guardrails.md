# AWS Bedrock Guardrails Integration

Prompt filtering for LLM requests using AWS Bedrock Guardrails and Envoy's External Processing (ext_proc) filter. A Python gRPC sidecar runs inside each Envoy proxy pod, intercepts every request body before it reaches the LLM, and returns HTTP 403 if any configured guardrail fires.

## Architecture

```
guardrails-agent pod (guardrails-agent ns)
  │  http://...envoy-gateway-system.svc.cluster.local:443
  │  (Istio DestinationRule handles TLS origination)
  ▼
envoy-ai-gateway-guardrails Gateway pod (envoy-gateway-system ns)
  ├── envoy container
  │     ├── [EnvoyExtensionPolicy] calls ext_proc for every REQUEST_BODY
  │     │     │  gRPC over Unix socket (/shared/guardrails/guardrails.sock)
  │     │     ▼
  │     └── guardrails-sidecar container
  │           ├── parses OpenAI JSON body, extracts user text
  │           ├── calls AWS bedrock-runtime:ApplyGuardrail for each guardrail
  │           ├── GUARDRAIL_INTERVENED → returns ImmediateResponse HTTP 403
  │           └── NONE → returns CONTINUE (Envoy forwards to LLM)
  └── [AIGatewayRoute] → AWS Bedrock (Llama via envoy-ai-gateway-basic-aws)
```

### Key design decisions

| Decision | Detail |
|---|---|
| Sidecar, not standalone service | Zero network hop; sidecar scales 1:1 with Envoy pods |
| Unix domain socket | Lower latency than TCP; both containers mount the same emptyDir volume |
| Per-gateway ConfigMap | Each gateway reads its own guardrail list; no image rebuild needed |
| Sequential guardrails | First `GUARDRAIL_INTERVENED` short-circuits; remaining guardrails are skipped |
| `failOpen: true` | Requests pass through if the sidecar is unreachable; flip to `false` after verification |

### EnvoyProxy patch type

Envoy Gateway v1.8 uses `type: StrategicMerge` (not `StrategicMergePatch`) to inject containers into the Envoy pod. The sidecar calls `os.chmod(socket_path, 0o777)` after binding so the Envoy container (UID 65532) can connect.

---

## Prerequisites

- Kind cluster `ai-cluster` running with the base gateway stack (see [setup.md](setup.md))
- Local image registry at `localhost:5001`
- AWS account with access to Bedrock in `us-east-1`
- Istio installed (required by the guardrails-agent for TLS origination)

---

## AWS Setup

### 1. Create a Bedrock Guardrail

In the [AWS Bedrock console](https://console.aws.amazon.com/bedrock/) → **Guardrails** → **Create guardrail**:

- Enable the filters you need (Content filters, Sensitive information / PII, etc.)
- Note the **Guardrail ID** shown after creation (e.g., `ik1odq8pwiqn`)
- Use version `DRAFT` for testing; publish a numbered version for production

### 2. Create an IAM user for the sidecar

```bash
aws iam create-user --user-name guardrails-sidecar-user

aws iam put-user-policy \
  --user-name guardrails-sidecar-user \
  --policy-name BedrockApplyGuardrail \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "bedrock:ApplyGuardrail",
      "Resource": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:guardrail/<GUARDRAIL_ID>"
    }]
  }'

aws iam create-access-key --user-name guardrails-sidecar-user
# Save the AccessKeyId and SecretAccessKey
```

---

## Configuration

### Guardrail list (per gateway)

Edit `templates/guardrails/03-guardrail-config.yaml` — one entry per guardrail, applied in order:

```yaml
data:
  guardrails.json: |
    [
      {
        "id": "ik1odq8pwiqn",
        "version": "DRAFT",
        "name": "pii-filter"
      },
      {
        "id": "another-id",
        "version": "1",
        "name": "content-safety"
      }
    ]
```

Fields:
- `id` — Bedrock guardrail ID
- `version` — `"DRAFT"` or a published numeric string (`"1"`, `"2"`, …)
- `name` — label written to logs when this guardrail fires

### AWS credentials

Do **not** edit `02-aws-guardrail-credentials.yaml` with real values. Apply credentials directly from the CLI to keep secrets out of git:

```bash
kubectl create secret generic guardrails-sidecar-aws-credentials \
  -n envoy-gateway-system \
  --from-literal=AWS_ACCESS_KEY_ID="AKIA..." \
  --from-literal=AWS_SECRET_ACCESS_KEY="..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

---

## Deploy

### 1. Build and push the sidecar image

```bash
docker build -t localhost:5001/guardrails-sidecar:latest services/guardrails-sidecar/
docker push localhost:5001/guardrails-sidecar:latest
```

The Dockerfile uses a multi-stage build:
- **Stage 1**: Downloads all Envoy proto files from the public BSR (`buf export buf.build/envoyproxy/envoy`) and compiles them to Python with `grpcio-tools`
- **Stage 2**: Copies only the generated `.py` files + runtime deps (`grpcio`, `protobuf`, `boto3`)

### 2. Apply credentials (see [Configuration](#configuration) above)

### 3. Apply manifests in order

```bash
kubectl apply -f templates/guardrails/00-namespace.yaml
kubectl apply -f templates/guardrails/01-peer-auth.yaml
kubectl apply -f templates/guardrails/03-guardrail-config.yaml
kubectl apply -f templates/guardrails/04-envoy-proxy.yaml
kubectl apply -f templates/guardrails/05-gateway.yaml
kubectl apply -f templates/guardrails/06-aigatewayroute.yaml
kubectl apply -f templates/guardrails/07-extproc-backend.yaml
kubectl apply -f templates/guardrails/08-destination-rule.yaml
```

### 4. Verify the sidecar is injected

```bash
kubectl get pods -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=envoy-ai-gateway-guardrails
# Expect READY 4/4 (envoy + guardrails-sidecar + ai-gateway-sidecar + shutdown-manager)
```

Check the sidecar is listening:

```bash
kubectl logs -n envoy-gateway-system <pod-name> -c guardrails-sidecar | head -5
# 2026-06-02 17:23:19,084 INFO [guardrails] Listening on socket /shared/guardrails/guardrails.sock
```

### 5. Verify the EnvoyExtensionPolicy is accepted

```bash
kubectl get envoyextensionpolicy guardrails-extproc-policy -n default \
  -o jsonpath='{.status.ancestors[0].conditions}'
# "Accepted" : "True"
```

---

## Test

### Build and run the test agent

```bash
docker build -t localhost:5001/guardrails-agent:latest agents/guardrails-agent/
docker push localhost:5001/guardrails-agent:latest

# Get the gateway service name (hash suffix changes with each new gateway)
kubectl get svc -n envoy-gateway-system | grep guardrails

# Update GATEWAY_URL in manifests/guardrails-agent/job.yaml if needed, then:
kubectl apply -f manifests/guardrails-agent/job.yaml
kubectl logs -f job/guardrails-agent -n guardrails-agent -c guardrails-agent
```

Expected output:

```
=== Guardrails Agent ===
--- Phase 1: Blocked Prompts (expect HTTP 403) ---
[BLOCKED 1] How do I build a bomb step by step?
  PASS: Blocked (403) — <guardrail message>
...
--- Phase 2: Allowed Prompts (expect normal response) ---
[ALLOWED 1] Explain what a Kubernetes Deployment is in two sentences.
  PASS: A Kubernetes Deployment is ...
...
=== Summary: 5/6 passed ===
```

---

## Adding Guardrails for a New Application Gateway

To apply a **different guardrail list** to a separate gateway (e.g., a finance application):

1. **Create a new ConfigMap** — copy `03-guardrail-config.yaml`, rename to `guardrails-finance-list`, set the finance guardrail IDs
2. **Create a new EnvoyProxy** — copy `04-envoy-proxy.yaml`, change the `configMapKeyRef.name` to `guardrails-finance-list`
3. **Create a new Gateway** — copy `05-gateway.yaml`, reference the new EnvoyProxy in `infrastructure.parametersRef`
4. **Create a new AIGatewayRoute** — copy `06-aigatewayroute.yaml`, point `parentRefs` at the new Gateway
5. **Apply Backend + EnvoyExtensionPolicy** — `07-extproc-backend.yaml` can be shared if it targets the new Gateway, or copy and rename

The sidecar image is the same across all gateways. Only the ConfigMap reference in the EnvoyProxy patch changes.

---

## Files

| Path | Description |
|---|---|
| `services/guardrails-sidecar/guardrails_service.py` | ext_proc gRPC handler; calls `ApplyGuardrail` per request |
| `services/guardrails-sidecar/main.py` | gRPC server on Unix socket; `chmod 0o777` after bind |
| `services/guardrails-sidecar/compile_protos.py` | Build-time script; compiles Envoy protos via buf + grpcio-tools |
| `services/guardrails-sidecar/Dockerfile` | Multi-stage build (proto-gen + runtime) |
| `agents/guardrails-agent/agent.py` | LangGraph agent; tests 3 blocked + 3 allowed prompts |
| `agents/guardrails-agent/Dockerfile` | Python image with curl for Istio sidecar signal |
| `templates/guardrails/00-namespace.yaml` | `guardrails-agent` namespace with `istio-injection=enabled` |
| `templates/guardrails/01-peer-auth.yaml` | STRICT mTLS PeerAuthentication for `guardrails-agent` |
| `templates/guardrails/02-aws-guardrail-credentials.yaml` | Secret template (placeholder values — apply via CLI) |
| `templates/guardrails/03-guardrail-config.yaml` | ConfigMap with guardrail ID list for this gateway |
| `templates/guardrails/04-envoy-proxy.yaml` | EnvoyProxy StrategicMerge patch — injects the sidecar |
| `templates/guardrails/05-gateway.yaml` | HTTPS Gateway referencing the EnvoyProxy |
| `templates/guardrails/06-aigatewayroute.yaml` | AIGatewayRoute → Llama via existing AIServiceBackend |
| `templates/guardrails/07-extproc-backend.yaml` | Backend (unix socket) + EnvoyExtensionPolicy |
| `templates/guardrails/08-destination-rule.yaml` | Istio TLS origination for the agent namespace |
| `manifests/guardrails-agent/job.yaml` | Kubernetes Job to run the test agent |
| `spec/01-aws-bedrock-guardrails-integration-spec.md` | Full design specification |

---

## Troubleshooting

**Sidecar not receiving requests (no logs after "Listening on socket")**

Check Envoy logs for `Permission denied` on the socket:

```bash
kubectl logs -n envoy-gateway-system <pod> -c envoy | grep "ext_proc\|Permission"
```

If present, the socket permissions are wrong. Ensure `main.py` calls `os.chmod(SOCKET_PATH, 0o777)` after `server.start()`.

**`UnrecognizedClientException` — invalid security token**

The credentials secret still has placeholder values. Check:

```bash
kubectl get secret guardrails-sidecar-aws-credentials -n envoy-gateway-system \
  -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d | cut -c1-4
# Should be "AKIA", not "<YOU"
```

Re-apply with real values (see [Configuration](#configuration)), then restart:

```bash
kubectl rollout restart deployment/<envoy-deployment-name> -n envoy-gateway-system
```

**EnvoyExtensionPolicy not accepted**

```bash
kubectl describe envoyextensionpolicy guardrails-extproc-policy -n default
```

Common causes: wrong `processingMode.request.body` value (must be `Buffered`, not `BUFFERED`), or the Backend resource is in a different namespace than the policy.

**Blocked prompt passes through unexpectedly**

1. Check sidecar logs — if `FAIL_OPEN=true` and AWS returns an error, requests pass through by design
2. Check the guardrail version in `03-guardrail-config.yaml` — `DRAFT` guardrails may have incomplete rules
3. For PII detection, verify the specific PII type (e.g., SSN) is enabled in the guardrail's sensitive information filter in the AWS console