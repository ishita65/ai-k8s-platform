# Istio Agent

Adds Istio to the cluster so every agent pod gets an `istio-proxy` sidecar. Rather than putting the gateway pods inside the mesh (envoy-over-envoy), traffic is secured with **one-way TLS**: the sidecar on the agent pod originates a TLS connection to a dedicated HTTPS gateway using a server cert signed by Istio's own CA. No sidecar is injected into the Envoy Gateway namespaces.

## Architecture

```
Agent Pod (default ns)
  └── istio-proxy sidecar
        │  intercepts outbound :443
        │  DestinationRule: mode SIMPLE
        │  verifies server cert via Istio root CA
        ▼
Envoy Gateway Pod (envoy-gateway-system, NO sidecar)
  └── terminates TLS using cert signed by Istio CA
        ▼
AWS Bedrock (upstream TLS)
```

## Files

| Path | Purpose |
|---|---|
| `agents/istio-agent/agent.py` | LangGraph agent (same logic as first-agent) |
| `agents/istio-agent/Dockerfile` | Container image |
| `agents/istio-agent/requirements.txt` | Python dependencies |
| `manifests/istio-agent/job.yaml` | Kubernetes Job with sidecar quit wrapper |
| `templates/istio/istiod-values.yaml` | Istio Helm values (Kind-tuned) |
| `templates/istio/gateway.yaml` | Dedicated HTTPS Gateway (port 443) + AIGatewayRoute |
| `templates/istio/destination-rule.yaml` | TLS origination rule for agent sidecar |
| `templates/istio/peer-auth.yaml` | PeerAuthentication policy |
| `templates/istio/gen-gateway-cert.sh` | Script to sign and store the server cert |

## Namespace Summary

| Namespace | Istio Injection | Role |
|---|---|---|
| `default` | Enabled | Agent pods — sidecars originate TLS |
| `envoy-gateway-system` | Disabled | Envoy data plane — terminates TLS via Secret |
| `envoy-ai-gateway-system` | Disabled | AI Gateway controller — management plane only |
| `istio-system` | Disabled | Istio control plane |

## Setup

### 1. Install Istio

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update

kubectl create namespace istio-system

helm upgrade --install istio-base istio/base \
  --namespace istio-system --version 1.24.6 --wait

helm upgrade --install istiod istio/istiod \
  --namespace istio-system --version 1.24.6 \
  --values templates/istio/istiod-values.yaml --wait

kubectl wait --timeout=3m -n istio-system deployment/istiod --for=condition=Available
```

Key settings in `templates/istio/istiod-values.yaml`:

| Setting | Value | Effect |
|---|---|---|
| `meshConfig.enableAutoMtls` | `true` | Auto-upgrades connections to mTLS between sidecar-bearing pods |
| `meshConfig.outboundTrafficPolicy.mode` | `ALLOW_ANY` | Sidecars can reach external FQDNs (AWS Bedrock) |
| `holdApplicationUntilProxyStarts` | `true` | Main container waits for sidecar to be ready |
| `enableNamespacesByDefault` | `false` | Injection opt-in via namespace label only |

### 2. Enable sidecar injection

Only the `default` namespace (where agents run) gets injection. Gateway namespaces are intentionally excluded.

```bash
kubectl label namespace default istio-injection=enabled
```

### 3. Generate the TLS certificate

Extracts Istio's root CA from `istio-system`, signs a wildcard server cert for `*.envoy-gateway-system.svc.cluster.local`, and stores it as a Kubernetes Secret in the `default` namespace.

```bash
bash templates/istio/gen-gateway-cert.sh
```

The wildcard SAN covers the hash-suffixed service name Envoy Gateway auto-generates (e.g. `envoy-default-envoy-ai-gateway-istio-<hash>.envoy-gateway-system.svc.cluster.local`).

### 4. Apply Istio manifests

```bash
# Dedicated HTTPS Gateway (port 443) + AIGatewayRoute reusing existing Bedrock backend
kubectl apply -f templates/istio/gateway.yaml

# TLS origination rule: sidecar upgrades http://:443 traffic to TLS, verifies with Istio CA
kubectl apply -f templates/istio/destination-rule.yaml

# PeerAuthentication: mesh-wide PERMISSIVE baseline, STRICT on default namespace
kubectl apply -f templates/istio/peer-auth.yaml
```

After applying the gateway, find the generated service name:

```bash
kubectl get svc -n envoy-gateway-system
# Look for: envoy-default-envoy-ai-gateway-istio-<hash>
```

Update `GATEWAY_URL` in `manifests/istio-agent/job.yaml`:

```
http://<service-name>.envoy-gateway-system.svc.cluster.local:443
```

Note the `http://` scheme — the Istio sidecar handles TLS origination transparently. The Python agent code requires no changes.

## Running

```bash
docker build -t istio-agent:latest agents/istio-agent
kind load docker-image istio-agent:latest --name ai-cluster
docker tag istio-agent:latest localhost:5001/istio-agent:latest
docker push localhost:5001/istio-agent:latest

kubectl apply -f manifests/istio-agent/job.yaml
```

The job pod starts with **2/2 containers** (application + `istio-proxy`). After the agent finishes, the shell wrapper in `job.yaml` sends `POST http://localhost:15020/quitquitquit` to the Istio pilot-agent, which triggers a graceful sidecar drain and exit — allowing the Job to reach `Completed` state.

```bash
# Verify job completed successfully
kubectl get job istio-agent
# Expected: STATUS Complete, COMPLETIONS 1/1

# View agent output
kubectl logs job/istio-agent -c istio-agent

# View sidecar access log (shows TLS handshakes)
kubectl logs job/istio-agent -c istio-proxy
```