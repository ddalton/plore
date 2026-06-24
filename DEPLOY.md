# Deploying plore on Kubernetes

This is a template for deploying plore onto any Kubernetes cluster. The stack is the same
everywhere — **pgvector** (vector registry), **Ollama** (the embedder), a **LiteLLM** gateway, a
**chat upstream** (taalas-proxy), **MinIO** (artifact offload), a one-shot **ingest** Job, and the
**plore-ui** Deployment. What changes between clusters is a handful of values (kube context, image
source/arch, gateway address, how AWC credentials are obtained); those are called out below.

Concrete, ready-to-run instantiations live under `deploy/<cluster>/` (e.g. `deploy/c1`, `deploy/c2`)
— each is a copy of this flow with its values filled in and driven by one `deploy/<cluster>/deploy.sh`.
This doc explains what that script does and why, so the process doesn't have to be rediscovered.

## Per-cluster values to set

| Value | Where | Notes |
|---|---|---|
| Kube context (`$CTX`) | `deploy.sh` | e.g. `kind-<name>` for kind, or your cloud context. |
| Namespace (`$NS`) | `deploy.sh`, `plore.yaml` | `plore` by default. |
| plore image | `plore.yaml`, `deploy.sh` | **Local build + `kind load`** for kind on the same arch, or a **registry image** for a remote cluster. Set `imagePullPolicy` to `Never` for the former. |
| Image architecture | build step | The image arch must match the nodes (e.g. arm64 for kind on an Apple-silicon host, amd64 for a typical cloud cluster). A mismatched image will fail to run. |
| Chat upstream placement | `plore.yaml` | `taalas-proxy` either runs in-namespace or in a separate `taalas` namespace, depending on the cluster. |
| Gateway address (`$GW_IP`) | `deploy.sh`, `hostAliases` in `plore.yaml` + `mint-awc-key.yaml` | The in-cluster IP serving the awc-core gateway host (`console.awc-core.poc.internal`). **Not stable across cluster rebuilds** — see gotchas. |
| AWC credentials | secret `plore-awc-creds` | Either **provided** (paste `client-id`/`client-secret`), or **minted at deploy time** from LDAP bootstrap creds (see step 2). |

## Prerequisites

- The target cluster is reachable: `kubectl --context $CTX get nodes`.
- awc-core is running on it (only needed for **real** API execution): `kubectl --context $CTX -n awc-core get pods`.
- The awc-core OpenAPI specs are available locally at `../awc-core/api` (override with `SPECS_DIR`).
- Docker can build/obtain the plore image, and the `taalas-proxy` image is available to the cluster.

## The flow (what `deploy.sh` automates)

### 0. Get the images into the cluster
The image arch must match the nodes. For a local kind cluster, build and load directly (no registry):
```bash
docker build -t plore:0.1.0 .
kind load docker-image plore:0.1.0 <taalas-proxy-image> --name <cluster>
```
For a remote cluster, build/push to your registry and reference that image in `plore.yaml`.

### 1. Namespace + config
Creates the `$NS` namespace, the `litellm-config` configmap (from `litellm/config.yaml`), and the
`specs-bundle` configmap. The bundle is generated from the awc-core OpenAPI YAMLs into
`deploy/<cluster>/specs-bundle.json` (a generated artifact — gitignored).

### 2. Mint an AWC access key  ← the non-obvious part
*(Only when AWC creds are minted rather than provided.)* awc-core sits behind Knox; calls need a JWT.
plore's `awc_auth.get_token()` exchanges an **access key** (`client_id`/`client_secret`) for a JWT,
but the access key itself must be minted first. The mint is a 3-step SSO flow (see
`awc-core/scripts/get-access-token.sh`):

1. `GET https://console.awc-core.poc.internal/` → returns a **knox SSO redirect** (to
   `knox.awc-core.poc.internal/.../websso`).
2. `curl -u <ldap-user>:<ldap-pass>` against that redirect → sets a **`hadoop-jwt`** cookie.
3. `POST /api/v0/auth/access-keys/credentials` (with the cookie) → returns `client_id` / `client_secret`.

The LDAP credentials live in the **`ldap-bootstrap-credentials`** secret (keys `username` /
`password`) in the `awc-core` (and `knox`) namespace. `deploy.sh` copies that secret into `$NS`,
runs `mint-awc-key.yaml` as a Job (it needs `hostAliases` for **both** `console.` and `knox.`
`awc-core.poc.internal` → the gateway IP, since CoreDNS can't resolve `*.poc.internal`), reads
`client_id`/`client_secret` back from the Job log, and stores them in the **`plore-awc-creds`**
secret (keys `client-id` / `client-secret`, which `plore.yaml`'s `secretKeyRef` expects).

### 3. Apply the stack
`kubectl --context $CTX apply -f deploy/<cluster>/plore.yaml` — pgvector, ollama (+ `ollama-pull`
for `embeddinggemma`), taalas-proxy, minio, litellm, the `plore-ingest` Job, and the `plore-ui`
Deployment/Service.

### 4. Wait, then ingest
Wait for the core deployments, then for `ollama-pull` to finish. **Re-apply** `plore.yaml`
afterwards to restart `plore-ingest` — see the gotcha below.

## Gotchas (learned the hard way)

- **`plore-ingest` fails with `openai.InternalServerError: no healthy upstream`** if it runs
  before `ollama-pull` has finished downloading `embeddinggemma` — litellm's embed route has no
  healthy target yet. Fix: wait for `job/ollama-pull` to complete, then re-create the ingest Job.
  `deploy.sh` does this; if doing it by hand, `kubectl delete job plore-ingest && kubectl apply`.
- **Gateway IP is not stable.** When the gateway is reached via an in-cluster ClusterIP, it changes
  on cluster rebuild. Find it with (adjust the service name to your ingress):
  ```bash
  kubectl --context $CTX -n istio-ingress get svc default-awc-istio -o jsonpath='{.spec.clusterIP}'
  ```
  Update `GW_IP` in `deploy.sh` and the `hostAliases` in `plore.yaml` (plore-ui) and
  `mint-awc-key.yaml` if it changed. Confirm the host routes there:
  `kubectl --context $CTX get httproute -A | grep console.awc-core`.
- **Embedding dimension must match the model.** The schema column width comes from `EMBED_DIM`
  (768 for EmbeddingGemma). Changing models means changing `EMBED_DIM` **and** recreating the
  table — `CREATE TABLE IF NOT EXISTS` will not widen an existing column:
  `psql -c "DROP TABLE IF EXISTS api_endpoint_registry;"` then re-ingest.
- **DB table names** are `api_endpoint_registry` and `service_catalog` (not `operations`).
  Endpoint columns: `microservice_name, http_method, endpoint_path, operation_id,
  semantic_description, embedding`.
- **Image arch must match the nodes.** Don't deploy an amd64 image to arm64 kind nodes (or vice
  versa) — it won't run. Build for the node arch, or `kind load` a local same-arch build.

## Sanity test (post-deploy)

```bash
CTX=<your-context>; NS=plore

# 1. Ingestion landed (endpoint count tracks the awc-core spec, ~90+; all embedded; 4 services).
#    Confirm the embedding dimension matches the model (e.g. 768 for EmbeddingGemma).
#    NOTE: re-ingest after the spec changes does NOT delete removed routes (upsert-only) —
#    truncate first if a path was renamed/removed: psql -c "truncate api_endpoint_registry; truncate service_catalog;"
kubectl --context $CTX -n $NS exec deploy/pgvector -- psql -U plore -d maui_registry -t -c \
  "select count(*) from api_endpoint_registry;
   select count(*) from api_endpoint_registry where embedding is not null;
   select max(vector_dims(embedding)) from api_endpoint_registry;
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
