# Fallback Agent

Runs the same task loop as `first-agent` but adds automatic failover: if the primary AWS Bedrock backend returns an error (HTTP 404, 409, 429, 500–504, or a connection/timeout failure), the agent re-sends the same prompt through the **same Envoy AI Gateway** using model `gpt-4o`, which is routed to `api.openai.com`. Both paths go through the gateway; the agent never handles credentials directly.

## Architecture

```
Agent Pod
  │
  ▼  model=us.meta.llama3-3-70b-instruct-v1:0
Envoy AI Gateway ──► AWS Bedrock (primary)
  │                        │ 404 / 409 / 429 / 5xx / timeout
  │                        ▼  (agent catches error, retries with gpt-4o)
  │  model=gpt-4o
  └────────────────► api.openai.com (fallback)
                     (BackendSecurityPolicy injects API key)
```

## Files

| Path | Purpose |
|---|---|
| `agents/fallback-agent/agent.py` | LangGraph agent with fallback logic |
| `agents/fallback-agent/requirements.txt` | Python dependencies |
| `agents/fallback-agent/Dockerfile` | Container image |
| `manifests/fallback-agent/job.yaml` | Kubernetes Job manifest |
| `manifests/fallback-agent/openai-gateway.yaml` | AIGatewayRoute + backend resources for OpenAI |
| `manifests/fallback-agent/openai-secret.yaml` | OpenAI API key Secret (edit before applying) |

## Gateway Resources (`openai-gateway.yaml`)

| Resource | Name | Purpose |
|---|---|---|
| `AIGatewayRoute` | `fallback-agent-openai` | Matches `x-ai-eg-model: gpt-4o`, routes to OpenAI |
| `AIServiceBackend` | `fallback-agent-openai` | Schema `OpenAI` — handles protocol translation |
| `BackendSecurityPolicy` | `fallback-agent-openai-credentials` | Injects `Authorization: Bearer <key>` from Secret |
| `Backend` | `fallback-agent-openai` | FQDN `api.openai.com:443` |
| `BackendTLSPolicy` | `fallback-agent-openai-tls` | System CA verification for `api.openai.com` |

## Prerequisites

- Gateway setup complete — see [setup.md](../setup.md).
- AWS Bedrock credentials applied.
- OpenAI API key available.
- `GATEWAY_URL` in `manifests/fallback-agent/job.yaml` updated with the generated service name.

## Running

### 1. Set the OpenAI API key

Edit `manifests/fallback-agent/openai-secret.yaml` and replace the placeholder:

```yaml
stringData:
  apiKey: "sk-..."   # your real OpenAI API key
```

### 2. Apply gateway manifests

The Secret must exist before the BackendSecurityPolicy reconciles:

```bash
kubectl apply -f manifests/fallback-agent/openai-secret.yaml
kubectl apply -f manifests/fallback-agent/openai-gateway.yaml
```

Verify resources are accepted:

```bash
kubectl get aigatewayroute,aiservicebackend,backendsecuritypolicy,backend -n default
# fallback-agent-openai entries should show STATUS=Accepted
```

### 3. Build and push the image

```bash
docker build -t fallback-agent:latest agents/fallback-agent
docker tag fallback-agent:latest localhost:5001/fallback-agent:latest
docker push localhost:5001/fallback-agent:latest
```

### 4. Run the Job

```bash
kubectl apply -f manifests/fallback-agent/job.yaml
kubectl logs -f job/fallback-agent
```

Each task reports which backend served it:

```
[1/4] Prompt: Explain Kubernetes in one short paragraph.
  Backend: Bedrock (us.meta.llama3-3-70b-instruct-v1:0)
  Response: ...

[2/4] Prompt: What are the key benefits of using Envoy as a service proxy?
  Bedrock failed (status=429): ...
  Falling back via gateway to OpenAI (gpt-4o).
  Backend: OpenAI fallback (gpt-4o)
  Response: ...

Summary: 3 via Bedrock, 1 via OpenAI fallback
```

### 5. Re-run the Job

```bash
kubectl delete job fallback-agent
kubectl apply -f manifests/fallback-agent/job.yaml
```

## Testing the Fallback Path

To force the fallback on every task, use a non-existent model ID (triggers a 404 → fallback on all requests):

```bash
kubectl delete job fallback-agent --ignore-not-found
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: fallback-agent
  namespace: default
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: fallback-agent
    spec:
      restartPolicy: Never
      containers:
        - name: fallback-agent
          image: localhost:5001/fallback-agent:latest
          imagePullPolicy: Always
          env:
            - name: GATEWAY_URL
              value: "http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local"
            - name: MODEL_ID
              value: "bad-model-id-404"   # triggers 404 → fallback
            - name: OPENAI_MODEL_ID
              value: "gpt-4o"
EOF
kubectl logs -f job/fallback-agent
```

The gateway returns `No matching route found` (404) for the unknown model; the agent logs `Bedrock failed (status=404)` and immediately retries via the `gpt-4o` route.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `GATEWAY_URL` | `http://envoy-default-...svc.cluster.local` | Envoy Gateway service URL |
| `MODEL_ID` | `us.meta.llama3-3-70b-instruct-v1:0` | Primary Bedrock model ID |
| `OPENAI_MODEL_ID` | `gpt-4o` | Fallback model ID (routed to OpenAI via gateway) |