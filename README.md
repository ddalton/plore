# plore

Intent-driven API router for the Cloudera **Anywhere Cloud (AWC)** platform. Natural-language
requests are resolved to the right REST operation via semantic retrieval over a **pgvector**
registry of OpenAPI operations, parameters are extracted by an LLM, mutating calls are gated
behind human approval, and execution happens against the platform — returning a processed
natural-language answer with durable, resumable sessions.

Orchestrated with **LangGraph**. The LLM is reached through a **LiteLLM** gateway:
- **chat** → Taalas `llama3.1-8B` via [taalas-proxy](https://github.com/ddalton/taalas-proxy)
- **embeddings** → self-hosted **Ollama `embeddinggemma`** (768-dim, asymmetric query/doc prompts) — Taalas has no embeddings API

This is **Phase I** of the MAUI agentic platform. Full design + diagrams: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Components
- **pgvector registry** (`plore/db.py`) — one row per OpenAPI operation (`api_endpoint_registry`,
  HNSW/cosine), plus `service_catalog` (per-service descriptions) and resolved `body_schema`.
- **Ingestion** (`plore/ingest.py`) — load specs, split per operation, build a full-description
  embedding, resolve `$ref` request bodies, embed, and upsert idempotently. Sources: a spec
  directory (`SPECS_DIR`), a JSON bundle, or **awc-mcp** (`--from-mcp`).
- **Agent 1 — Router** (`plore/graphs/router.py`):
  `triage → optimize → retrieve → extract → gather_params → [approval gate] → execute → respond`.
  `GET/HEAD/OPTIONS` auto-execute; mutating methods pause at a LangGraph **interrupt** (HITL).
  Meta/"what can you do?" questions are answered conversationally instead of forcing an endpoint.
- **Agent 2 — Discovery** (`plore/graphs/discovery.py`): read-only "which endpoint do I call?".
- **Execution & auth** (`plore/awc_auth.py`) — calls the AWC/Knox gateway with a Bearer JWT
  minted from an access key (`client_credentials`), cached and refreshed.
- **Parameter completion** — `gather_params` scaffolds the request body from the spec `example`
  and flags missing required fields; the UI lets you edit the body before approving.
- **Artifact offload** (`plore/artifacts.py`) — binary responses (e.g. diagnostic bundles) are
  streamed to **MinIO/S3**; only a reference (key + presigned URL) enters the session.
- **Durable sessions** (`plore/checkpoint.py`) — a Postgres LangGraph checkpointer (reuses the
  pgvector DB); sessions/threads + the HITL interrupt survive restarts and are resumable by id.
- **UI** (`ui/app.py`) — Streamlit: chat, full trace, inline approve/reject with an editable body,
  and a durable session history.
- **LiteLLM** (`litellm/config.yaml`) + **Ollama** embedder, wired in `docker-compose.yaml`.

No FastAPI: graphs are served by the **LangGraph server** (`langgraph dev`, see `langgraph.json`),
which provides threads/interrupts/resume; a thin **CLI** is provided for local runs.

## Quick start (local)
```bash
pip install -e ".[dev,ui]"

# 1. bring up the stack (vector DB, embedder, chat proxy, gateway)
docker compose up -d pgvector ollama taalas-proxy litellm
docker compose exec ollama ollama pull embeddinggemma   # first run only (768-dim embedder)

# 2. ingest the AWC OpenAPI specs into pgvector
#    local dev: read the spec files directly
SPECS_DIR=../awc-core/api plore-ingest
#    in-cluster alternative (files aren't on the cluster — pull specs from awc-mcp):
#    AWC_MCP_URL=http://awc-mcp:8080/mcp plore-ingest

# 3a. read-only discovery
plore-discovery "how do I list deployed clusters?"

# 3b. intent router (dry-run unless AWC_API_BASE is set; pauses for approval on writes)
plore-router "create a cluster named demo"

# 3c. or serve both graphs over HTTP
langgraph dev

# 3d. or use the UI
streamlit run ui/app.py          # http://localhost:8501
```

## Deploy to Kubernetes (kind cluster `c2`)
```bash
deploy/c2/deploy.sh   # build + kind load, generate spec bundle, configmaps, apply, wait
kubectl --context kind-c2 -n plore port-forward svc/plore-ui 8501:8501
```
Brings up pgvector, Ollama, LiteLLM, MinIO, the UI, and ingestion in namespace `plore`, reusing
the `taalas-proxy` in the `taalas` namespace and executing against `awc-core` via the Knox
gateway. See [ARCHITECTURE.md](ARCHITECTURE.md) §11 for the topology and §7 for the auth flow.
> Note: the `plore` image is built locally and `kind load`ed (`imagePullPolicy: Never`); it is
> not published to a registry.

## Configuration
All env-driven; see `.env.example`. Key variables:

| Area | Variables |
|---|---|
| DB / retrieval | `DATABASE_URL`, `TOP_K` (default 5), `PROJECT_ID` |
| LLM gateway | `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `CHAT_MODEL`, `EMBED_MODEL`, `EMBED_DIM` |
| Ingestion | `SPECS_DIR`, `AWC_MCP_URL`, `ENRICH_DESCRIPTIONS` |
| Execution / auth | `AWC_API_BASE`, `AWC_API_VERIFY_TLS`, `AWC_API_TOKEN`, `AWC_ACCESS_KEY_ID`, `AWC_ACCESS_KEY_SECRET` |
| Artifacts | `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`, `MINIO_SECURE` |

## Safety & execution
- The router auto-executes only `GET`/`HEAD`/`OPTIONS`. Any mutating call **interrupts** and waits
  for explicit approve/reject; the approval can also supply/override the request body
  (`Command(resume={"approved": true, "body": {...}})`).
- With `AWC_API_BASE` unset, execution is a **dry run** that returns the resolved call without
  sending it. With it set, calls go to the AWC/Knox gateway authenticated with a minted JWT.
- Artifact offload activates only when `MINIO_ENDPOINT` is set (the cluster deploy); otherwise
  binary responses are summarized inline.
