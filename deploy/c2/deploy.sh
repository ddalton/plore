#!/usr/bin/env bash
# Deploy plore onto the c2 kind cluster.
set -euo pipefail
cd "$(dirname "$0")/../.."

CTX=kind-c2
NS=plore

# 1. Build the image and load it into the kind cluster (no registry needed).
docker build -t plore:0.1.0 .
kind load docker-image plore:0.1.0 --name c2

# 2. Namespace + config (litellm config + the OpenAPI specs bundle).
# Generate the specs bundle from the awc-core OpenAPI files (not committed).
SPECS_DIR=${SPECS_DIR:-../awc-core/api}
python3 - "$SPECS_DIR" > deploy/c2/specs-bundle.json <<'PY'
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
  --from-file=specs-bundle.json=deploy/c2/specs-bundle.json \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -

# 3. Apply the stack.
kubectl --context $CTX apply -f deploy/c2/plore.yaml

# 4. Wait for core services, then ingestion.
kubectl --context $CTX -n $NS rollout status deploy/pgvector --timeout=180s
kubectl --context $CTX -n $NS rollout status deploy/ollama --timeout=180s
kubectl --context $CTX -n $NS rollout status deploy/litellm --timeout=180s
kubectl --context $CTX -n $NS wait --for=condition=complete job/ollama-pull --timeout=600s
kubectl --context $CTX -n $NS wait --for=condition=complete job/plore-ingest --timeout=600s

echo "Done. UI:  kubectl --context $CTX -n $NS port-forward svc/plore-ui 8501:8501"
