# Kubernetes AI Platform

A local [Kind](https://kind.sigs.k8s.io/) cluster that runs AI agents and proxies LLM requests through [Envoy AI Gateway](https://aigateway.envoyproxy.io/) to AWS Bedrock and OpenAI.

## Architecture

Requests reach the cluster through the `envoy-ai-gateway-basic` Gateway. The AI Gateway routes by the `x-ai-eg-model` header value to the appropriate backend:

```
Agent Pod (default ns)
  │  OpenAI-compatible POST /v1/chat/completions
  │  model: <model-id>
  ▼
Envoy AI Gateway (envoy-gateway-system)
  │  reads model field → sets x-ai-eg-model header
  │  AIGatewayRoute selects AIServiceBackend
  │  BackendSecurityPolicy injects credentials
  ▼
AWS Bedrock / OpenAI
```

| `x-ai-eg-model` header | Backend | Schema |
|---|---|---|
| `us.meta.llama3-3-70b-instruct-v1:0` | AWS Bedrock (Meta Llama) | `AWSBedrock` |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | AWS Bedrock (Anthropic) | `AWSAnthropic` |
| `gpt-4o` | OpenAI (fallback agent only) | `OpenAI` |

## Project Status

| Goal | Status |
|---|---|
| Deploy Envoy Gateway + Envoy AI Gateway on Kind | Done |
| Connect to AWS Bedrock (Meta Llama + Anthropic Claude) | Done |
| First agent via Envoy AI Gateway | Done |
| Fallback agent (Bedrock → OpenAI) | Done |
| Token-based rate limiting | Done |
| Istio mTLS sidecar integration | Done |

## Documentation

| Doc | Description |
|---|---|
| [docs/setup.md](docs/setup.md) | Cluster bootstrap — Envoy Gateway, AWS credentials, Redis |
| [docs/agents/first-agent.md](docs/agents/first-agent.md) | LangGraph agent → AWS Bedrock (Llama) |
| [docs/agents/fallback-agent.md](docs/agents/fallback-agent.md) | Bedrock primary with OpenAI fallback |
| [docs/agents/istio-agent.md](docs/agents/istio-agent.md) | Agent with Istio sidecar and one-way TLS |
| [docs/rate-limiting.md](docs/rate-limiting.md) | Token-based rate limiting via Redis |

## Quick Start

```bash
# 1. Install Envoy Gateway + Envoy AI Gateway (see docs/setup.md for full command)
helm upgrade -i eg oci://docker.io/envoyproxy/gateway-helm --version v1.8.0 \
  --namespace envoy-gateway-system --create-namespace \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/manifests/envoy-gateway-values.yaml \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/examples/token_ratelimit/envoy-gateway-values-addon.yaml \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/examples/inference-pool/envoy-gateway-values-addon.yaml

helm upgrade -i aieg-crd oci://docker.io/envoyproxy/ai-gateway-crds-helm --version v0.6.0 \
  --namespace envoy-ai-gateway-system --create-namespace

helm upgrade -i aieg oci://docker.io/envoyproxy/ai-gateway-helm --version v0.6.0 \
  --namespace envoy-ai-gateway-system --create-namespace

# 2. Apply AWS credentials + gateway manifests
# Edit templates/aws-bedrock/sample.yaml with real credentials first
kubectl apply -f templates/aws-bedrock/sample.yaml

# 3. Run the first agent
docker build -t localhost:5001/first-agent:latest agents/first-agent
docker push localhost:5001/first-agent:latest
kubectl apply -f manifests/first-agent/job.yaml
kubectl logs -f job/first-agent
```

## Repository Layout

```
agents/
  first-agent/      # Bedrock agent (Llama via AWSBedrock schema)
  fallback-agent/   # Bedrock primary + OpenAI fallback
  istio-agent/      # Same as first-agent but runs inside Istio mesh
manifests/
  first-agent/      # Kubernetes Job
  fallback-agent/   # Job + OpenAI gateway resources
  istio-agent/      # Job with sidecar quit wrapper
  rate-limiting/    # BackendTrafficPolicy for token budget
templates/
  aws-bedrock/      # Gateway, AIGatewayRoute, AIServiceBackend, credentials Secret
  istio/            # Helm values, TLS cert script, gateway/DR/peer-auth manifests
  redis.yaml        # Redis for rate-limit state
docs/               # Detailed documentation
```
