# Token-Based Rate Limiting

Enforces a **100 total-token-per-hour** budget on the first-agent's Llama model route. The limit is intentionally tight for testing — a single LLM exchange typically exceeds 100 tokens, so the agent hits the ceiling after one or two calls and receives a `429 Too Many Requests` from the gateway.

## How It Works

```
first-agent request (x-ai-eg-model: us.meta.llama3-3-70b-instruct-v1:0)
       │
       ▼
Envoy AI Gateway
  ├── AIGatewayRoute (llmRequestCosts) ──► extracts token count from Bedrock response
  │                                         writes to io.envoy.ai_gateway/llm_total_token
  │
  └── BackendTrafficPolicy (Global rate limit)
        ├── clientSelector: x-ai-eg-model Distinct
        │     → isolated 100-token/hour bucket per model value
        ├── cost.request: 0       (no budget consumed on the request leg)
        └── cost.response: reads llm_total_token metadata → deducts actual usage
              │
              ▼
           Redis (redis-system:6379) — stores shared token counter
```

Three components work together:

1. **`llmRequestCosts` in `AIGatewayRoute`** (`templates/aws-bedrock/sample.yaml`) — instructs the AI Gateway controller to extract the total token count from each Bedrock response and publish it as `io.envoy.ai_gateway/llm_total_token` dynamic metadata.

2. **`BackendTrafficPolicy`** (`manifests/rate-limiting/rate-limit.yaml`) — targets the `envoy-ai-gateway-basic` Gateway. Uses `x-ai-eg-model: Distinct` as the client selector so each model value gets its own isolated bucket (100 tokens/hour). The `cost.response.from: Metadata` field reads the token count from the metadata key above to deduct actual usage from the budget.

3. **Redis** (`templates/redis.yaml`, namespace `redis-system`) — backs the global rate limit state. Must be running before the rate-limit addon is effective.

## Files

| Path | Purpose |
|---|---|
| `manifests/rate-limiting/rate-limit.yaml` | `BackendTrafficPolicy` — enforces the token budget |
| `templates/aws-bedrock/sample.yaml` | `AIGatewayRoute` with `llmRequestCosts` — extracts token metadata |
| `templates/redis.yaml` | Redis deployment in `redis-system` namespace |

## Prerequisites

- Envoy Gateway installed with the rate-limit addon Helm values (included in the [setup command](./setup.md)).
- Redis deployed:

```bash
kubectl apply -f templates/redis.yaml
kubectl wait --timeout=2m -n redis-system deployment/redis --for=condition=Available
```

## Applying the Rate Limit

```bash
# Update AIGatewayRoute with llmRequestCosts (token metadata extraction)
kubectl apply -f templates/aws-bedrock/sample.yaml

# Create the BackendTrafficPolicy (token budget enforcement)
kubectl apply -f manifests/rate-limiting/rate-limit.yaml
```

Verify both resources are accepted:

```bash
kubectl get backendtrafficpolicy first-agent-token-ratelimit -n default
kubectl get aigatewayroute envoy-ai-gateway-basic-aws -n default
```

## Testing

Run the first-agent job:

```bash
kubectl delete job first-agent --ignore-not-found
kubectl apply -f manifests/first-agent/job.yaml
kubectl logs -f job/first-agent
```

Expected output — the agent completes one or two tasks then hits the 100-token ceiling:

```
[1/4] Prompt: Explain Kubernetes in one short paragraph.
Response: Kubernetes is an open-source container orchestration system...

[2/4] Prompt: What are the key benefits of using Envoy as a service proxy?
Response: ...

[3/4] Prompt: How does AWS Bedrock simplify deploying foundation models...
openai.RateLimitError: Error code: 429
```

Re-running within the same hour immediately returns a `429` because the budget is already exhausted:

```bash
kubectl delete job first-agent
kubectl apply -f manifests/first-agent/job.yaml
kubectl logs -f job/first-agent
# → 429 on the very first call
```

Confirm Redis is holding the token counter:

```bash
kubectl exec -n redis-system deploy/redis -- redis-cli keys "*"
```

## Adjusting the Budget

Edit `manifests/rate-limiting/rate-limit.yaml` and change `limit.requests`:

```yaml
limit:
  requests: 100   # increase for normal usage, e.g. 10000
  unit: Hour
```

Then reapply:

```bash
kubectl apply -f manifests/rate-limiting/rate-limit.yaml
```