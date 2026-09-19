# Gateway Setup

Steps to install Envoy Gateway, Envoy AI Gateway, Redis, and the AWS Bedrock manifests on a local Kind cluster.

## Prerequisites

- [kind](https://kind.sigs.k8s.io/) cluster running (named `ai-cluster`)
- `kubectl` and `helm` available
- AWS credentials for Bedrock (see [AWS Credentials](#aws-credentials))
- Local image registry at `localhost:5001` (used by agents)

## 1. Install Envoy Gateway and Envoy AI Gateway

Install in order — Envoy Gateway must be ready before Envoy AI Gateway.

```bash
helm upgrade -i eg oci://docker.io/envoyproxy/gateway-helm --version v1.8.0 \
  --namespace envoy-gateway-system --create-namespace \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/manifests/envoy-gateway-values.yaml \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/examples/token_ratelimit/envoy-gateway-values-addon.yaml \
  -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/examples/inference-pool/envoy-gateway-values-addon.yaml

helm upgrade -i aieg-crd oci://docker.io/envoyproxy/ai-gateway-crds-helm --version v0.6.0 \
  --namespace envoy-ai-gateway-system --create-namespace

helm upgrade -i aieg oci://docker.io/envoyproxy/ai-gateway-helm --version v0.6.0 \
  --namespace envoy-ai-gateway-system --create-namespace

kubectl wait --timeout=2m -n envoy-ai-gateway-system deployment/ai-gateway-controller \
  --for=condition=Available

kubectl wait pods --timeout=2m \
  -l gateway.envoyproxy.io/owning-gateway-name=envoy-ai-gateway-basic \
  -n envoy-gateway-system --for=condition=Ready
```

The third Helm values file (`token_ratelimit`) enables the rate-limit addon — required for [token rate limiting](./rate-limiting.md).

## 2. AWS Credentials

Edit `templates/aws-bedrock/sample.yaml` and replace the placeholder values with real credentials:

```yaml
stringData:
  credentials: |
    [default]
    aws_access_key_id = <YOUR_ACCESS_KEY>
    aws_secret_access_key = <YOUR_SECRET_KEY>
```

Then apply the full manifest (Gateway, AIGatewayRoute, AIServiceBackend, BackendSecurityPolicy, Backend, BackendTLSPolicy):

```bash
kubectl apply -f templates/aws-bedrock/sample.yaml
```

For EKS, replace the credential block with an IRSA annotation instead.

## 3. Deploy Redis (for Rate Limiting)

Redis is required only when the token rate-limit feature is used. Skip if you are not enabling [rate limiting](./rate-limiting.md).

```bash
kubectl apply -f templates/redis.yaml
kubectl wait --timeout=2m -n redis-system deployment/redis --for=condition=Available
```

## 4. Find the Gateway Service Name

Envoy Gateway generates a service name with a hash suffix. You need this URL for agent jobs.

```bash
kubectl get svc -n envoy-gateway-system
# Look for: envoy-default-envoy-ai-gateway-basic-<hash>
```

The full in-cluster URL is:
```
http://envoy-default-envoy-ai-gateway-basic-<hash>.envoy-gateway-system.svc.cluster.local
```

Update the `GATEWAY_URL` env var in each agent's job manifest with this value.

## Request Routing

Requests reach the cluster through the `envoy-ai-gateway-basic` Gateway. The AI Gateway routes by the `x-ai-eg-model` header:

| Header value | Backend | Schema |
|---|---|---|
| `us.meta.llama3-3-70b-instruct-v1:0` | AWS Bedrock (Meta Llama) | `AWSBedrock` |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | AWS Bedrock (Anthropic) | `AWSAnthropic` |
| `gpt-4o` | OpenAI (fallback agent only) | `OpenAI` |