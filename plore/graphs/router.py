"""Agent 1 — Intent-Driven API Router.

Flow:  triage -> retrieve (agentic) -> extract_params -> (approval_gate) -> execute
GET/HEAD/OPTIONS are auto-executed; mutating methods pause at a LangGraph interrupt
(the Approval Gate, MAUI guide §6) and resume with the human's decision.
"""

from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import httpx
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .. import artifacts, awc_auth, llm
from ..config import config
from .common import (
    agentic_retrieve,
    candidate_views,
    parse_json_object,
    service_catalog_lines,
)

_EXTRACT_SYSTEM = (
    "You are a Parameter Extraction Agent for the AWC platform. Given a user request and a "
    "numbered list of candidate API operations (each with a description), choose the SINGLE best "
    "operation BY ITS id — the one whose description performs the user's intended action.\n"
    "Match the user's ACTION to the right operation (a small model tends to over-match keywords):\n"
    "- create / add / provision / launch / deploy / new / spin up  -> the operation that CREATES\n"
    "- list / show / view / get / find  -> the GET that lists/returns\n"
    "- delete / remove / destroy  -> the DELETE\n"
    "- update / change / modify  -> the PUT/PATCH\n"
    "Never pick a list/GET for a create request, even if it shares keywords.\n"
    "Respond with ONLY a JSON object of this exact shape:\n"
    '{"id": <integer id of the chosen candidate, or null if none fit>, '
    '"path_params": {}, "query_params": {}, "body": {}}\n'
    "Do NOT output a method or path — only the id. Fill path_params for any {placeholders} in the "
    "chosen candidate's path; include only values you can infer from the request. If none of the "
    "candidates can satisfy the request, set id to null. No prose."
)


_TRIAGE_SYSTEM = (
    "You are the triage step for 'plore', an assistant that turns natural-language requests into "
    "AWC (Anywhere Cloud) platform REST API calls. Classify the user's message into exactly one:\n"
    '- "api_action": the user wants to look up, list, query, create, modify, or operate on AWC '
    "resources (clusters, applications, auth/SSO, service accounts, diagnostics, data access, etc.).\n"
    '- "meta": a question about the assistant itself or its capabilities, a greeting, or general '
    "conversation that does NOT require calling an API.\n"
    'Respond with ONLY {"kind": "api_action"} or {"kind": "meta"}.'
)

_META_SYSTEM = (
    "You are plore, an assistant that turns natural-language requests into AWC platform API calls "
    "and (with approval) executes them. The platform exposes these services:\n{services}\n"
    "Answer the user's message conversationally and concisely, grounded in the service descriptions "
    "above — describe what you can help with and give a couple of example requests. You may include "
    "code or examples if the user asks for them. Do not invent services beyond those listed."
)


class RouterState(TypedDict, total=False):
    query: str
    intent: str
    candidates: list[dict[str, Any]]
    proposed_call: dict[str, Any]
    approved: bool
    missing_required: list[str]
    result: dict[str, Any]
    error: str
    response: str  # final natural-language answer for the user
    # Durable, append-only record of each turn in the session (persisted via checkpointer).
    # Artifact references (e.g. stored download URLs) will be attached here too.
    session_log: Annotated[list[dict[str, Any]], operator.add]


def _node_triage(state: RouterState) -> RouterState:
    reply = llm.chat(
        [
            {"role": "system", "content": _TRIAGE_SYSTEM},
            {"role": "user", "content": state["query"]},
        ],
        max_tokens=32,
    )
    try:
        kind = parse_json_object(reply).get("kind")
    except ValueError:
        kind = None
    # Default to api_action so plore's primary function is preserved on parse failure.
    return {"intent": "meta" if kind == "meta" else "api_action"}


def _node_meta(state: RouterState) -> RouterState:
    try:
        services = "\n".join(service_catalog_lines()) or "the AWC platform APIs"
    except Exception:  # noqa: BLE001 - meta answer must not depend on the registry being up
        services = "the AWC platform APIs"
    answer = llm.chat(
        [
            {"role": "system", "content": _META_SYSTEM.format(services=services)},
            {"role": "user", "content": state["query"]},
        ],
        max_tokens=300,
    )
    return {
        "response": answer,
        "session_log": [{"query": state.get("query"), "kind": "meta", "response": answer}],
    }


def _node_retrieve(state: RouterState) -> RouterState:
    return {"candidates": candidate_views(agentic_retrieve(state["query"]))}


def _node_extract(state: RouterState) -> RouterState:
    ops = state.get("candidates") or []
    if not ops:
        return {"error": "no candidate endpoints found"}
    # Present candidates with explicit ids; the model selects one (it cannot invent a path/method).
    # Concise listing for selection: the full description is great for embedding/recall but
    # overwhelms a small model here ("lost in the middle"). Prefer the terse summary.
    listing = [
        {"id": i, "service": c["service"], "method": c["method"], "path": c["path"],
         "summary": c.get("summary") or (c.get("description") or "")[:120],
         "parameters": c.get("parameters"), "has_body": c.get("has_body")}
        for i, c in enumerate(ops)
    ]
    reply = llm.chat(
        [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": f"User request:\n{state['query']}\n\nCandidate operations:\n"
                + json.dumps(listing, indent=2),
            },
        ],
        max_tokens=400,
    )
    try:
        proposed = parse_json_object(reply)
    except ValueError as exc:
        return {"error": str(exc)}
    idx = proposed.get("id")
    if not isinstance(idx, int) or idx < 0 or idx >= len(ops):
        return {"error": "No registered endpoint matches this request."}
    chosen = ops[idx]
    # method and path are taken VERBATIM from the retrieved candidate — never model-authored.
    return {
        "proposed_call": {
            "service": chosen["service"],
            "method": str(chosen["method"]).upper(),
            "path": chosen["path"],
            "path_params": proposed.get("path_params") or {},
            "query_params": proposed.get("query_params") or {},
            "body": proposed.get("body") or {},
        }
    }


def _node_gather_params(state: RouterState) -> RouterState:
    """Scaffold the request body from the operation's example, overlay any extracted values,
    and flag still-missing required fields (option A — example-as-defaults)."""
    call = state.get("proposed_call") or {}
    schema = next(
        (c.get("body_schema") for c in (state.get("candidates") or [])
         if c.get("method") == call.get("method") and c.get("path") == call.get("path")),
        None,
    )
    if not schema:
        return {}  # operation takes no JSON body
    example = schema.get("example") if isinstance(schema.get("example"), dict) else {}
    extracted = call.get("body") if isinstance(call.get("body"), dict) else {}
    body = {**(example or {}), **extracted}  # example defaults, user/model values win
    missing = [f for f in (schema.get("required") or [])
               if f not in body or body.get(f) in (None, "")]
    return {"proposed_call": {**call, "body": body}, "missing_required": missing}


def _node_approval_gate(state: RouterState) -> RouterState:
    # Pauses the run; resume with Command(resume={"approved": bool, "body": {...}?}).
    decision = interrupt(
        {
            "type": "approval_required",
            "proposed_call": state.get("proposed_call"),
            "missing_required": state.get("missing_required") or [],
            "prompt": "Review/complete the body, then approve. "
            "Resume with {'approved': true|false, 'body': {...}}.",
        }
    )
    call = state.get("proposed_call") or {}
    if isinstance(decision, dict):
        approved = decision.get("approved")
        if isinstance(decision.get("body"), dict):
            call = {**call, "body": decision["body"]}
    else:
        approved = bool(decision)
    return {"approved": bool(approved), "proposed_call": call}


def _node_execute(state: RouterState) -> RouterState:
    call = state.get("proposed_call") or {}
    method = call.get("method", "GET")
    path = call.get("path", "")
    for key, value in (call.get("path_params") or {}).items():
        path = path.replace(f"{{{key}}}", str(value))

    if not config.awc_api_base:
        return {"result": {"status": "dry_run", "would_call": {**call, "resolved_path": path}}}

    url = config.awc_api_base.rstrip("/") + path
    headers = {"Accept": "application/json, */*"}
    token = awc_auth.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.request(
            method,
            url,
            params=call.get("query_params") or None,
            json=call.get("body") or None,
            headers=headers,
            verify=config.awc_api_verify_tls,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the caller
        return {"error": f"execution failed: {exc}"}

    result: dict[str, Any] = {"status": resp.status_code, "url": url, "method": method}
    ctype = resp.headers.get("content-type", "").lower()
    disp = resp.headers.get("content-disposition", "")
    is_binary = bool(
        "attachment" in disp
        or (ctype and not ctype.startswith("text/") and "json" not in ctype)
        or len(resp.content) > 200_000
    )

    if "application/json" in ctype:
        try:
            result["body"] = resp.json()
        except Exception:  # noqa: BLE001
            result["body"] = resp.text[:2000]
    elif is_binary and resp.is_success:
        filename = _artifact_filename(disp, path)
        key = f"downloads/{uuid4().hex}-{filename}"
        result["artifact"] = artifacts.offload(
            resp.content, key, ctype or "application/octet-stream"
        )
    else:
        result["body"] = resp.text[:2000]
    return {"result": result}


def _artifact_filename(content_disposition: str, path: str) -> str:
    m = re.search(r'filename\*?=(?:"([^"]+)"|([^;]+))', content_disposition)
    if m:
        return (m.group(1) or m.group(2)).strip().split("/")[-1]
    base = path.rstrip("/").split("/")[-1] or "artifact"
    return base if "." in base else base + ".bin"


def _node_rejected(state: RouterState) -> RouterState:
    return {"result": {"status": "rejected", "proposed_call": state.get("proposed_call")}}


_RESPOND_SYSTEM = (
    "You are an AWC assistant. Using ONLY the information provided (the user's request, the API "
    "call that was made or proposed, and its result), write a clear, concise natural-language "
    "answer for the user. If the result is a dry run, explain what would be called. If there was "
    "an error or the action was rejected, say so plainly. Summarize result data; do not invent "
    "anything not present in the result."
)


def _node_respond(state: RouterState) -> RouterState:
    payload = {
        "user_request": state.get("query"),
        "api_call": state.get("proposed_call"),
        "result": state.get("result"),
        "error": state.get("error"),
    }
    answer = llm.chat(
        [
            {"role": "system", "content": _RESPOND_SYSTEM},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ],
        max_tokens=400,
    )
    return {
        "response": answer,
        "session_log": [
            {
                "query": state.get("query"),
                "kind": "api_action",
                "proposed_call": state.get("proposed_call"),
                "result": state.get("result"),
                "error": state.get("error"),
                "response": answer,
            }
        ],
    }


def _route_after_extract(state: RouterState) -> str:
    return "respond" if state.get("error") else "gather_params"


def _route_after_gather(state: RouterState) -> str:
    method = (state.get("proposed_call") or {}).get("method", "GET")
    return "execute" if method in config.safe_methods else "approval_gate"


def _route_after_gate(state: RouterState) -> str:
    return "execute" if state.get("approved") else "rejected"


def _route_after_triage(state: RouterState) -> str:
    return "meta" if state.get("intent") == "meta" else "retrieve"


def build_graph(checkpointer=None):
    g = StateGraph(RouterState)
    g.add_node("triage", _node_triage)
    g.add_node("meta", _node_meta)
    g.add_node("retrieve", _node_retrieve)
    g.add_node("extract", _node_extract)
    g.add_node("gather_params", _node_gather_params)
    g.add_node("approval_gate", _node_approval_gate)
    g.add_node("execute", _node_execute)
    g.add_node("rejected", _node_rejected)
    g.add_node("respond", _node_respond)

    g.set_entry_point("triage")
    g.add_conditional_edges("triage", _route_after_triage,
                            {"meta": "meta", "retrieve": "retrieve"})
    g.add_edge("meta", END)
    g.add_edge("retrieve", "extract")
    g.add_conditional_edges("extract", _route_after_extract,
                            {"gather_params": "gather_params", "respond": "respond"})
    g.add_conditional_edges("gather_params", _route_after_gather,
                            {"execute": "execute", "approval_gate": "approval_gate"})
    g.add_conditional_edges("approval_gate", _route_after_gate,
                            {"execute": "execute", "rejected": "rejected"})
    g.add_edge("execute", "respond")
    g.add_edge("rejected", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)


# For the LangGraph server (persistence/interrupts provided by the platform).
graph = build_graph()
