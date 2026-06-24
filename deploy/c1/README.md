# Deploying plore on the `c1` kind cluster

`c1` is a self-contained local **kind** cluster that runs the full **awc-core** platform
(Knox gateway, awc-console, awc-auth, diagnostics) under an Istio **ambient** mesh. This is the
cluster to use when you want plore to execute **real** API calls against awc-core, end to end.

> One command: `./deploy/c1/deploy.sh`. The rest of this doc explains what it does and why, so
> the process doesn't have to be rediscovered each time.

## How c1 differs from c2

| | c2 | c1 |
|---|---|---|
| Chat upstream | `taalas-proxy` in a separate `taalas` namespace | `taalas-proxy` runs **in the `plore` namespace** (no `taalas` ns on c1) |
| plore image | `docker-sandbox.infra.cloudera.com/ddalton/plore:0.1.0` (amd64, registry) | local **arm64** `plore:0.1.0` via `kind load` (`imagePullPolicy: Never`) |
| Gateway host IP | `10.96.9.243` | **`10.96.59.175`** (c1 istio-ingress `default-awc` ClusterIP) |
| AWC creds | provided | **minted at deploy time** from the LDAP bootstrap creds |

The ClusterIP of the istio-ingress gateway changes per cluster rebuild — re-check it (see below)
if a deploy fails to reach `console.awc-core.poc.internal`.

## Prerequisites

- `kind get clusters` shows `c1`; `kubectl config get-contexts` has `kind-c1`.
- awc-core is already running on c1 (`kubectl --context kind-c1 -n awc-core get pods`).
- The awc-core OpenAPI specs are checked out at `../awc-core/api` (override with `SPECS_DIR`).
- Local Docker can build the plore image; `dilipdalton/taalas-proxy:0.1.0` is present locally
  (`docker image ls | grep taalas`).

## The flow (what `deploy.sh` automates)

### 0. Load images into the cluster
c1 nodes are arm64 (kind on an arm64 Mac), so the **local** arm64 `plore:0.1.0` build is the
right image — not the amd64 one pushed to the registry for the real Cloudera infra cluster.
```bash
docker build -t plore:0.1.0 .
kind load docker-image plore:0.1.0 dilipdalton/taalas-proxy:0.1.0 --name c1
```

### 1. Namespace + config
Creates the `plore` namespace, the `litellm-config` configmap (from `litellm/config.yaml`), and
the `specs-bundle` configmap. The bundle is generated from the awc-core OpenAPI YAMLs into
`deploy/c1/specs-bundle.json`.

### 2. Mint an AWC access key  ← the non-obvious part
awc-core sits behind Knox; calls need a JWT. plore's `awc_auth.get_token()` exchanges an
**access key** (`client_id`/`client_secret`) for a JWT, but the access key itself must be minted
first. The mint is a 3-step SSO flow (see `awc-core/scripts/get-access-token.sh`):

1. `GET https://console.awc-core.poc.internal/` → returns a **knox SSO redirect** (to
   `knox.awc-core.poc.internal/.../websso`).
2. `curl -u <ldap-user>:<ldap-pass>` against that redirect → sets a **`hadoop-jwt`** cookie.
3. `POST /api/v0/auth/access-keys/credentials` (with the cookie) → returns `client_id` /
   `client_secret`.

The LDAP credentials live in the **`ldap-bootstrap-credentials`** secret (keys `username` /
`password`) in the `awc-core` (and `knox`) namespace. `deploy.sh` copies that secret into `plore`,
runs `mint-awc-key.yaml` as a Job (it needs `hostAliases` for **both** `console.` and `knox.`
`awc-core.poc.internal` → the gateway IP, since CoreDNS can't resolve `*.poc.internal`), reads
`client_id`/`client_secret` back from the Job log, and stores them in the **`plore-awc-creds`**
secret (keys `client-id` / `client-secret`, which `plore.yaml`'s `secretKeyRef` expects).

### 3. Apply the stack
`kubectl apply -f deploy/c1/plore.yaml` — pgvector, ollama (+ `ollama-pull` for `embeddinggemma`),
taalas-proxy, minio, litellm, the `plore-ingest` Job, and the `plore-ui` Deployment/Service.

### 4. Wait, then ingest
Wait for the core deployments, then for `ollama-pull` to finish. **Re-apply** `plore.yaml`
afterwards to restart `plore-ingest` — see the gotcha below.

## Gotchas (learned the hard way)

- **`plore-ingest` fails with `openai.InternalServerError: no healthy upstream`** if it runs
  before `ollama-pull` has finished downloading `embeddinggemma` — litellm's embed route has no
  healthy target yet. Fix: wait for `job/ollama-pull` to complete, then re-create the ingest Job.
  `deploy.sh` does this; if doing it by hand, `kubectl delete job plore-ingest && kubectl apply`.
- **Gateway ClusterIP is not stable.** Find it with:
  ```bash
  kubectl --context kind-c1 -n istio-ingress get svc default-awc-istio -o jsonpath='{.spec.clusterIP}'
  ```
  Update `GW_IP` in `deploy.sh`, the `hostAliases` in `plore.yaml` (plore-ui) and
  `mint-awc-key.yaml` if it changed. Confirm the host routes there:
  `kubectl --context kind-c1 get httproute -A | grep console.awc-core`.
- **DB table names** are `api_endpoint_registry` and `service_catalog` (not `operations`).
  Endpoint columns: `microservice_name, http_method, endpoint_path, operation_id,
  semantic_description, embedding`.
- **arch**: don't deploy the amd64 registry image to c1 — it won't run on arm64 kind nodes. Use
  the local build + `kind load`.

## Sanity test (post-deploy)

```bash
CTX=kind-c1; NS=plore

# 1. Ingestion landed (endpoint count tracks the awc-core spec, ~90+; all embedded; 4 services).
#    NOTE: re-ingest after the spec changes does NOT delete removed routes (upsert-only) —
#    truncate first if a path was renamed/removed: psql -c "truncate api_endpoint_registry; truncate service_catalog;"
kubectl --context $CTX -n $NS exec deploy/pgvector -- psql -U plore -d maui_registry -t -c \
  "select count(*) from api_endpoint_registry;
   select count(*) from api_endpoint_registry where embedding is not null;
   select count(*) from service_catalog;"

# 2. End-to-end discovery (vector recall + LLM selection via taalas).
kubectl --context $CTX -n $NS exec deploy/plore-ui -- \
  plore-discovery "how do I list deployed clusters?"
#   -> selects GET /api/v0/console/clusters

# 3. Real AWC execution through the Knox gateway (mint JWT -> call awc-console).
kubectl --context $CTX -n $NS exec deploy/plore-ui -- python3 -c "
from plore import awc_auth; from plore.config import config; import httpx
tok = awc_auth.get_token()
r = httpx.get(config.awc_api_base.rstrip('/') + '/api/v0/console/clusters',
              headers={'Authorization': 'Bearer ' + tok}, verify=config.awc_api_verify_tls, timeout=20)
print(r.status_code, r.text[:200])"
#   -> 200 [] (no clusters deployed yet)

# 4. UI reachable.
kubectl --context $CTX -n $NS port-forward svc/plore-ui 8501:8501
#   open http://localhost:8501 ; health: curl -s localhost:8501/_stcore/health
```
