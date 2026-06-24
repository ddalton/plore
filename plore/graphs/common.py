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
    raw = llm.chat(
        [
            {"role": "system", "content": QUERY_OPTIMIZER_SYSTEM},
            {"role": "user", "content": query},
        ],
        max_tokens=128,
    )
    return _clean_optimized(raw, fallback=query)


def _clean_optimized(text: str, fallback: str) -> str:
    """Small models often ignore 'output only' and emit numbered reasoning. Extract just the
    optimized string (prefer a final quoted value), and fall back to the raw query if unsure."""
    if not text or not text.strip():
        return fallback
    import re

    quoted = re.findall(r'"([^"]+)"', text)
    if quoted:
        cand = quoted[-1].strip()
    else:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        cand = lines[-1] if lines else ""
        cand = re.sub(r"^(\d+[.)]\s*)?(output:?\s*)?", "", cand, flags=re.I).strip().strip('"')
    # Reject obviously-bad extractions (empty, too long, leftover step markers).
    if not cand or len(cand) > 200 or re.match(r"^\d+[.)]\s", cand):
        return fallback
    return cand


def retrieve(optimized_query: str) -> list[db.Candidate]:
    vec = llm.embed_query(optimized_query)
    conn = db.connect()
    try:
        return db.search(conn, vec, project_id=config.project_id, k=config.top_k)
    finally:
        conn.close()


# --- Agentic retrieval: retrieve -> evaluate -> reformulate ---------------------------------
# Control is owned by agentic_retrieve() below (deterministic, bounded). The LLM only performs two
# narrow judgments a small model can manage: grade whether candidates are relevant, and suggest an
# alternate phrasing. A blind one-shot rewrite (optimize_query) can silently replace a good query
# with a bad one; the loop unions candidates across phrasings so a good hit can never be dropped.

_RELEVANCE_SYSTEM = (
    "You check retrieval quality. Given a user request and candidate API operations (method, path, "
    "summary), decide whether AT LEAST ONE candidate can satisfy the request. "
    'Respond with ONLY {"relevant": true} or {"relevant": false}.'
)

_REFORMULATE_SYSTEM = (
    "You reformulate a search query for a technical OpenAPI vector registry. The phrasings already "
    "tried did NOT surface a relevant endpoint. Given the user request and those phrasings, write "
    "ONE different short search phrase that emphasizes other keywords or synonyms (resource nouns, "
    "the HTTP action). Output ONLY the phrase — no preamble, no quotes."
)


def _merge_candidates(groups: list[list[db.Candidate]]) -> list[db.Candidate]:
    """Union candidates from several retrievals: dedupe by (method, path), keep the smallest
    distance, return sorted ascending by distance."""
    best: dict[tuple[str, str], db.Candidate] = {}
    for group in groups:
        for c in group:
            key = (c.http_method, c.endpoint_path)
            if key not in best or c.distance < best[key].distance:
                best[key] = c
    return sorted(best.values(), key=lambda c: c.distance)


def _grade_relevant(query: str, views: list[dict[str, Any]]) -> bool:
    """LLM yes/no: can any candidate satisfy the request? Terse listing (same shape the selector
    uses). Fail-open on parse error so the loop can't spin on a flaky reply."""
    if not views:
        return False
    listing = [
        {"method": v["method"], "path": v["path"],
         "summary": v.get("summary") or (v.get("description") or "")[:120]}
        for v in views
    ]
    reply = llm.chat(
        [
            {"role": "system", "content": _RELEVANCE_SYSTEM},
            {"role": "user", "content": f"Request:\n{query}\n\nCandidates:\n"
             + json.dumps(listing, indent=2)},
        ],
        max_tokens=16,
    )
    try:
        return bool(parse_json_object(reply).get("relevant"))
    except ValueError:
        return True


def _reformulate(query: str, tried: list[str]) -> str:
    reply = llm.chat(
        [
            {"role": "system", "content": _REFORMULATE_SYSTEM},
            {"role": "user", "content": f"User request:\n{query}\n\nPhrasings already tried:\n"
             + "\n".join(f"- {t}" for t in tried)},
        ],
        max_tokens=64,
    )
    return _clean_optimized(reply, fallback=query)


def agentic_retrieve(query: str) -> list[db.Candidate]:
    """Bounded retrieve->evaluate->reformulate loop. Retrieves on the raw query first (best recall),
    accepts immediately on a clearly-close hit, else brings in the optimized phrasing and an LLM
    relevance grader, and reformulates up to retrieval_max_iters times. Returns the unioned top_k."""
    if not config.agentic_retrieval:
        return retrieve(optimize_query(query))

    floor = config.retrieval_distance_floor
    tried = [query]
    groups = [retrieve(query)]
    merged = _merge_candidates(groups)[: config.top_k]
    # Fast path: a close raw-query hit needs no LLM and no loop.
    if merged and merged[0].distance <= floor:
        return merged

    # Ambiguous: add the optimized (HyDE-style) phrasing and grade the union.
    opt = optimize_query(query)
    if opt and opt not in tried:
        tried.append(opt)
        groups.append(retrieve(opt))
        merged = _merge_candidates(groups)[: config.top_k]
    if (merged and merged[0].distance <= floor) or _grade_relevant(query, candidate_views(merged)):
        return merged

    for _ in range(config.retrieval_max_iters):
        alt = _reformulate(query, tried)
        if not alt or alt in tried:
            break
        tried.append(alt)
        groups.append(retrieve(alt))
        merged = _merge_candidates(groups)[: config.top_k]
        if (merged and merged[0].distance <= floor) or _grade_relevant(query, candidate_views(merged)):
            break
    return merged


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
        v["body_schema"] = c.body_schema  # {required, properties, example} or None
        views.append(v)
    return views


def service_catalog_lines() -> list[str]:
    """Grounded per-service lines ('- <title>: <first line of description>') for meta answers,
    sourced from each spec's OpenAPI info block at ingestion time."""
    conn = db.connect()
    try:
        catalog = db.service_catalog(conn, project_id=config.project_id)
    finally:
        conn.close()
    lines = []
    for name, title, description in catalog:
        label = title or name
        summary = description.strip().split("\n")[0].strip() if description else ""
        lines.append(f"- {label}: {summary}" if summary else f"- {label}")
    return lines


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
