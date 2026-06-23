"""Central configuration, all env-driven (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # Postgres / pgvector
    database_url: str = _env(
        "DATABASE_URL", "postgresql://plore:plore@localhost:5432/maui_registry"
    )

    # LiteLLM gateway (OpenAI-compatible). Chat routes to taalas-proxy; embeddings to TEI.
    litellm_base_url: str = _env("LITELLM_BASE_URL", "http://localhost:4000/v1")
    litellm_api_key: str = _env("LITELLM_API_KEY", "sk-plore-local")
    chat_model: str = _env("CHAT_MODEL", "taalas-llama")
    embed_model: str = _env("EMBED_MODEL", "bge-small")
    embed_dim: int = int(_env("EMBED_DIM", "384"))

    # Retrieval
    top_k: int = int(_env("TOP_K", "3"))
    project_id: str = _env("PROJECT_ID", "awc")

    # Ingestion sources (priority: --from-mcp / AWC_MCP_URL, else --bundle, else SPECS_DIR).
    #  - specs_dir: directory with <service>/openapi.yaml (local dev only).
    #  - awc_mcp_url: awc-mcp streamable-HTTP endpoint; specs fetched via get_api_specs
    #    (cluster-native — the files are NOT on the cluster, awc-mcp serves them).
    specs_dir: str = _env("SPECS_DIR", "")
    awc_mcp_url: str = _env("AWC_MCP_URL", "")
    awc_mcp_token: str = _env("AWC_MCP_TOKEN", "")

    # Execution: one common gateway base URL (empty → dry-run). All AWC services are
    # path-routed behind it (/api/v0/console, /api/v0/auth, /api/v1/diagnostics).
    awc_api_base: str = _env("AWC_API_BASE", "")
    awc_api_verify_tls: bool = _env("AWC_API_VERIFY_TLS", "false").lower() == "true"
    # Auth (Knox enforces at the gateway): a ready JWT, or an access key (client_id/secret)
    # plore exchanges for one at the open /api/v0/auth/access-keys/token path (Knox-exempt).
    awc_api_token: str = _env("AWC_API_TOKEN", "")
    awc_access_key_id: str = _env("AWC_ACCESS_KEY_ID", "")
    awc_access_key_secret: str = _env("AWC_ACCESS_KEY_SECRET", "")

    # Artifact offload (S3/MinIO). Empty endpoint → binary responses are summarized inline.
    minio_endpoint: str = _env("MINIO_ENDPOINT", "")  # host:port (no scheme)
    minio_access_key: str = _env("MINIO_ACCESS_KEY", "")
    minio_secret_key: str = _env("MINIO_SECRET_KEY", "")
    minio_bucket: str = _env("MINIO_BUCKET", "plore-artifacts")
    minio_secure: bool = _env("MINIO_SECURE", "false").lower() == "true"

    # Methods that are auto-executed without a human approval gate.
    safe_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")


config = Config()
