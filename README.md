# plore

Intent-driven API router for AWC. Natural-language requests are resolved to the right REST
operation via semantic retrieval over a **pgvector** registry of OpenAPI operations, parameters
are extracted by an LLM, and (gated) execution happens against the platform. Orchestrated with
**LangGraph**; the LLM is reached through **LiteLLM** (chat → the Taalas `llama3.1-8B` model via
[taalas-proxy](https://github.com/ddalton/taalas-proxy); embeddings → a self-hosted
`bge-small-en-v1.5`).

This is Phase I of the MAUI agentic platform. See `~/Documents/plore-phase1-plan.md`.

## Components
- **pgvector registry** (`db/schema.sql`) — one row per OpenAPI operation (`api_endpoint_registry`).
- **Ingestion** (`plore/ingest.py`) — load specs, split per operation, embed, upsert (idempotent).
- **Agent 1 — Router** (`plore/graphs/router.py`): `optimize → retrieve → extract → approval → execute`.
  GET/HEAD/OPTIONS auto-execute; mutating methods pause at a LangGraph **interrupt** (HITL gate).
- **Agent 2 — Discovery** (`plore/graphs/discovery.py`): read-only "which endpoint do I call?".
- **UI** (`ui/app.py`) — Streamlit: enter a query, watch optimize→retrieve→extract→execute,
  approve/reject mutating calls inline, and read the processed natural-language response.
- **LiteLLM** (`litellm/config.yaml`) and **TEI** embedder, wired in `docker-compose.yaml`.

No FastAPI: graphs are served by the **LangGraph server** (`langgraph dev`, see `langgraph.json`),
which provides threads, interrupts and resume; a thin **CLI** is provided for local runs.

## Quick start (local)
```bash
pip install -e ".[dev]"

# 1. bring up the stack (vector DB, embedder, chat proxy, gateway)
docker compose up -d pgvector ollama taalas-proxy litellm
docker compose exec ollama ollama pull all-minilm   # first run only (384-dim embedder)

# 2. ingest the AWC OpenAPI specs into pgvector
#    local dev: read the spec files directly
SPECS_DIR=../awc-core/api plore-ingest
#    in-cluster: the files aren't on the cluster — pull specs from awc-mcp instead
#    AWC_MCP_URL=http://awc-mcp:8080/mcp plore-ingest   (--from-mcp)

# 3a. read-only discovery
plore-discovery "how do I list deployed clusters?"

# 3b. intent router (dry-run unless AWC_API_BASE is set; pauses for approval on writes)
plore-router "create a cluster named demo"

# 3c. or serve both graphs over HTTP
langgraph dev

# 3d. or use the UI
pip install -e ".[ui]"
streamlit run ui/app.py          # http://localhost:8501
```

## Configuration
All env-driven; see `.env.example`. Key vars: `DATABASE_URL`, `LITELLM_BASE_URL`,
`CHAT_MODEL`, `EMBED_MODEL`, `EMBED_DIM`, `TOP_K`, `PROJECT_ID`, `SPECS_DIR`, `AWC_API_BASE`,
`AWC_API_TOKEN`.

## Safety
The router auto-executes only `GET`/`HEAD`/`OPTIONS`. Any mutating call interrupts and waits for an
explicit approve/reject (resume with `Command(resume={"approved": true})`). With `AWC_API_BASE`
unset, execution is a **dry run** that returns the resolved call without sending it.
