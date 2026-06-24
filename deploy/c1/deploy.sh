#!/usr/bin/env bash
# Deploy plore onto the c1 kind cluster.
#
# c1 is a self-contained local kind cluster running awc-core (Knox gateway, awc-console,
# awc-auth, diagnostics). Unlike c2 there is no separate `taalas` namespace, so taalas-proxy
# runs in the plore namespace. The plore + taalas-proxy images are the local arm64 builds
# loaded straight into the cluster (no registry pull).
set -euo pipefail
cd "$(dirname "$0")/../.."

CTX=kind-c1
NS=plore
GW_IP=${GW_IP:-10.96.59.175}   # istio-ingress (default-awc) ClusterIP serving console.awc-core.poc.internal

# 0. Load the local images into the cluster (arm64; matches the kind nodes on an arm64 host).
docker build -t plore:0.1.0 .
kind load docker-image plore:0.1.0 dilipdalton/taalas-proxy:0.1.0 --name c1

# 1. Namespace + config (litellm config + the OpenAPI specs bundle).
# Generate the specs bundle from the awc-core OpenAPI files (not committed).
SPECS_DIR=${SPECS_DIR:-../awc-core/api}
python3 - "$SPECS_DIR" > deploy/c1/specs-bundle.json <<'PY'
import json, pathlib, sys
api = pathlib.Path(sys.argv[1]).expanduser()
print(json.dumps({p.parent.name: {"content": p.read_text()}
                  for p in sorted(api.glob("*/openapi.y*ml"))}))
PY

kubectl --context $CTX create namespace $NS --dry-run=client -o yaml | kubectl --context $CTX apply -f -
kubectl --context $CTX -n $NS create configmap litellm-config \
  --from-file=config.yaml=litellm/config.yaml \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -
kubectl --context $CTX -n $NS create configmap specs-bundle \
  --from-file=specs-bundle.json=deploy/c1/specs-bundle.json \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -

# 2. Mint an AWC access key (client_id/client_secret) and store it as plore-awc-creds.
# Copy the LDAP bootstrap creds into the namespace so the mint Job can SSO-authenticate.
kubectl --context $CTX -n awc-core get secret ldap-bootstrap-credentials -o json \
  | python3 -c "import sys,json; s=json.load(sys.stdin); print(json.dumps({'apiVersion':'v1','kind':'Secret','metadata':{'name':'ldap-bootstrap-credentials','namespace':'$NS'},'type':'Opaque','data':s['data']}))" \
  | kubectl --context $CTX apply -f -
kubectl --context $CTX -n $NS delete job mint-awc-key --ignore-not-found
kubectl --context $CTX apply -f deploy/c1/mint-awc-key.yaml
kubectl --context $CTX -n $NS wait --for=condition=complete job/mint-awc-key --timeout=180s
LOGS=$(kubectl --context $CTX -n $NS logs job/mint-awc-key)
CID=$(echo "$LOGS" | sed -n 's/^RESULT_CLIENT_ID=//p')
CSEC=$(echo "$LOGS" | sed -n 's/^RESULT_CLIENT_SECRET=//p')
[ -n "$CID" ] && [ -n "$CSEC" ] || { echo "FATAL: access key not minted"; echo "$LOGS"; exit 1; }
kubectl --context $CTX -n $NS create secret generic plore-awc-creds \
  --from-literal=client-id="$CID" --from-literal=client-secret="$CSEC" \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -
kubectl --context $CTX -n $NS delete job mint-awc-key --ignore-not-found
kubectl --context $CTX -n $NS delete secret ldap-bootstrap-credentials --ignore-not-found

# 3. Apply the stack.
kubectl --context $CTX apply -f deploy/c1/plore.yaml

# 4. Wait for core services, then ingestion.
kubectl --context $CTX -n $NS rollout status deploy/pgvector --timeout=180s
kubectl --context $CTX -n $NS rollout status deploy/ollama --timeout=180s
kubectl --context $CTX -n $NS rollout status deploy/taalas-proxy --timeout=180s
kubectl --context $CTX -n $NS rollout status deploy/litellm --timeout=180s
kubectl --context $CTX -n $NS wait --for=condition=complete job/ollama-pull --timeout=600s
# Ingest depends on the embed model being pulled; re-apply once ollama-pull is done so it
# doesn't exhaust its backoff racing the model download.
kubectl --context $CTX -n $NS delete job plore-ingest --ignore-not-found
kubectl --context $CTX apply -f deploy/c1/plore.yaml
kubectl --context $CTX -n $NS wait --for=condition=complete job/plore-ingest --timeout=600s

echo "Done. UI:  kubectl --context $CTX -n $NS port-forward svc/plore-ui 8501:8501"
