"""LLM access via the LiteLLM gateway (OpenAI-compatible).

Chat is routed by LiteLLM to taalas-proxy (llama3.1-8B); embeddings to Ollama
EmbeddingGemma. Both are reached with the standard OpenAI client.

EmbeddingGemma is asymmetric — embed corpus text with `embed_documents()` and
search queries with `embed_query()`, which prepend the model's task prompts
(config.embed_doc_prefix / embed_query_prefix). Ollama's embed API does not add
these automatically, so we prepend them here.
"""

from __future__ import annotations

from openai import OpenAI

from .config import config

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
    resp = client().embeddings.create(model=config.embed_model, input=texts)
    return [d.embedding for d in resp.data]


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed corpus documents with the document task prompt prepended."""
    return embed([config.embed_doc_prefix + t for t in texts])


def embed_query(text: str) -> list[float]:
    """Embed a single search query with the query task prompt prepended."""
    return embed([config.embed_query_prefix + text])[0]


def chat(messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None) -> str:
    """Single completion; returns the assistant message content."""
    resp = client().chat.completions.create(
        model=config.chat_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()
