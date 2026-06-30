#!/usr/bin/env bash
# Generates a server TLS certificate for the Envoy AI Gateway (istio gateway),
# signed by Istio's root CA, and stores it as a Kubernetes Secret.
#
# The certificate SAN uses a wildcard for envoy-gateway-system so it matches
# the hash-suffixed service name Envoy Gateway auto-generates for any listener
# (e.g. envoy-default-envoy-ai-gateway-istio-<hash>.envoy-gateway-system.svc.cluster.local).
#
# Prerequisites: kubectl (connected to the cluster), openssl
#
# Usage:
#   bash templates/istio/gen-gateway-cert.sh
#
# Creates/updates Secret envoy-ai-gateway-istio-tls in the default namespace.
# This Secret is referenced by the Gateway TLS listener in gateway.yaml.

set -euo pipefail

NAMESPACE="default"
SECRET_NAME="envoy-ai-gateway-istio-tls"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "==> Extracting Istio root CA from istio-system..."
kubectl get secret istio-ca-secret -n istio-system \
  -o jsonpath='{.data.ca-cert\.pem}' | base64 -d > "$TMPDIR/ca.crt"
kubectl get secret istio-ca-secret -n istio-system \
  -o jsonpath='{.data.ca-key\.pem}' | base64 -d > "$TMPDIR/ca.key"

echo "==> Generating server private key (RSA 2048)..."
openssl genrsa -out "$TMPDIR/server.key" 2048

cat > "$TMPDIR/server.cnf" <<EOF
[req]
req_extensions     = v3_req
distinguished_name = req_distinguished_name
prompt             = no
[req_distinguished_name]
CN = envoy-ai-gateway-istio
O  = istio-system
[v3_req]
keyUsage         = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = @alt_names
[alt_names]
DNS.1 = *.envoy-gateway-system.svc.cluster.local
DNS.2 = *.envoy-gateway-system
EOF

echo "==> Generating CSR..."
openssl req -new \
  -key    "$TMPDIR/server.key" \
  -out    "$TMPDIR/server.csr" \
  -config "$TMPDIR/server.cnf"

echo "==> Signing certificate with Istio root CA (valid 365 days)..."
openssl x509 -req \
  -in         "$TMPDIR/server.csr" \
  -CA         "$TMPDIR/ca.crt" \
  -CAkey      "$TMPDIR/ca.key" \
  -CAcreateserial \
  -out        "$TMPDIR/server.crt" \
  -days       365 \
  -extensions v3_req \
  -extfile    "$TMPDIR/server.cnf"

echo "==> Verifying certificate chain..."
openssl verify -CAfile "$TMPDIR/ca.crt" "$TMPDIR/server.crt"
openssl x509 -noout -text -in "$TMPDIR/server.crt" | grep -A2 "Subject Alternative Name"

echo "==> Creating/updating Secret '$SECRET_NAME' in namespace '$NAMESPACE'..."
kubectl create secret tls "$SECRET_NAME" \
  --cert="$TMPDIR/server.crt" \
  --key="$TMPDIR/server.key" \
  --namespace="$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "Done. Next steps:"
echo "  1. Apply Gateway + AIGatewayRoute:"
echo "       kubectl apply -f templates/istio/gateway.yaml"
echo ""
echo "  2. Apply DestinationRule (TLS origination at sidecar):"
echo "       kubectl apply -f templates/istio/destination-rule.yaml"
echo ""
echo "  3. Find the auto-generated service name for the new gateway:"
echo "       kubectl get svc -n envoy-gateway-system"
echo "     Look for envoy-default-envoy-ai-gateway-istio-<hash>"
echo ""
echo "  4. Update GATEWAY_URL in manifests/istio-agent/job.yaml with:"
echo "       http://<service-name>.envoy-gateway-system.svc.cluster.local:443"
echo "     (http:// scheme, port 443 — sidecar handles TLS origination)"
