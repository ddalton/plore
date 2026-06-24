"""LLM access via the LiteLLM gateway (OpenAI-compatible).

Chat is routed by LiteLLM to taalas-proxy (llama3.1-8B); embeddings to Ollama
EmbeddingGemma. Both are reached with the standard OpenAI client.

EmbeddingGemma is asymmetric — embed corpus text with `embed_documents()` and
search queries with `embed_query()`, which prepend the model's task prompts
(config.embed_doc_prefix / embed_query_prefix). Ollama's embed API does not add
these automatically, so we prepend them here.
"""

from __future__ import annotations

import time

from openai import OpenAI

from .config import config
from .obs import get_logger

_log = get_logger("plore.llm")

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.litellm_base_url, api_key=config.litellm_api_key)
    return _client


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts (raw, no task prompt); returns one vector per input."""
    if not texts:
        return []
    t0 = time.perf_counter()
    try:
        resp = client().embeddings.create(model=config.embed_model, input=texts)
    except Exception as exc:  # noqa: BLE001 - log then re-raise so the failure is captured
        _log.error("embed call FAILED model=%s n=%d err=%s", config.embed_model, len(texts), exc)
        raise
    _log.debug("embed ok model=%s n=%d ms=%.0f", config.embed_model, len(texts),
               (time.perf_counter() - t0) * 1000)
    return [d.embedding for d in resp.data]


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed corpus documents with the document task prompt prepended."""
    return embed([config.embed_doc_prefix + t for t in texts])


def embed_query(text: str) -> list[float]:
    """Embed a single search query with the query task prompt prepended."""
    return embed([config.embed_query_prefix + text])[0]


def chat(messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None) -> str:
    """Single completion; returns the assistant message content."""
    t0 = time.perf_counter()
    try:
        resp = client().chat.completions.create(
            model=config.chat_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - log then re-raise so the failure is captured
        _log.error("chat call FAILED model=%s msgs=%d err=%s", config.chat_model, len(messages), exc)
        raise
    out = (resp.choices[0].message.content or "").strip()
    _log.debug("chat ok model=%s msgs=%d ms=%.0f out_len=%d", config.chat_model, len(messages),
               (time.perf_counter() - t0) * 1000, len(out))
    return out
