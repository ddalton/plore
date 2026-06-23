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

    # Ingestion: directory of OpenAPI specs (e.g. awc-core/api with <service>/openapi.yaml).
    specs_dir: str = _env("SPECS_DIR", "")

    # Execution: base URL for resolved AWC API calls + bearer token (service-account JWT).
    awc_api_base: str = _env("AWC_API_BASE", "")
    awc_api_token: str = _env("AWC_API_TOKEN", "")

    # Methods that are auto-executed without a human approval gate.
    safe_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")


config = Config()
