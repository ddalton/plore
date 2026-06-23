"""Nodes and helpers shared by the router and discovery graphs."""

from __future__ import annotations

import json
from typing import Any

from .. import db, llm
from ..config import config

# MAUI guide §4 — Query Optimizer system prompt.
QUERY_OPTIMIZER_SYSTEM = (
    "You are a Query Optimization Agent. Translate a raw user request into a concise, "
    "semantic search query for a technical OpenAPI vector registry.\n"
    "INSTRUCTIONS:\n"
    "1. Strip conversational filler.\n"
    "2. Resolve ambiguous/temporal expressions where possible.\n"
    "3. Draft a short declarative sentence predicting the technical capability required.\n"
    "4. Output ONLY the optimized query string, no code, no preamble."
)


def optimize_query(query: str) -> str:
    optimized = llm.chat(
        [
            {"role": "system", "content": QUERY_OPTIMIZER_SYSTEM},
            {"role": "user", "content": query},
        ],
        max_tokens=128,
    )
    return optimized or query


def retrieve(optimized_query: str) -> list[db.Candidate]:
    vec = llm.embed_one(optimized_query)
    conn = db.connect()
    try:
        return db.search(conn, vec, project_id=config.project_id, k=config.top_k)
    finally:
        conn.close()


def compact_operation(c: db.Candidate) -> dict[str, Any]:
    """A small, token-bounded view of an operation for the extractor prompt."""
    op = c.raw_openapi_json or {}
    params = [
        {
            "name": p.get("name"),
            "in": p.get("in"),
            "required": p.get("required", False),
            "type": (p.get("schema") or {}).get("type"),
        }
        for p in op.get("parameters", [])
        if isinstance(p, dict)
    ]
    return {
        "service": c.microservice_name,
        "method": c.http_method,
        "path": c.endpoint_path,
        "summary": op.get("summary") or c.semantic_description,
        "parameters": params,
        "has_body": bool(op.get("requestBody")),
    }


def candidate_views(candidates: list[db.Candidate]) -> list[dict[str, Any]]:
    """Serializable, token-bounded candidate dicts for state + the extractor prompt."""
    views = []
    for c in candidates:
        v = compact_operation(c)
        v["operation_id"] = c.operation_id
        v["description"] = c.semantic_description
        v["distance"] = round(c.distance, 4)
        views.append(v)
    return views


def parse_json_object(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an LLM reply (handles code fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(text[start : end + 1])
