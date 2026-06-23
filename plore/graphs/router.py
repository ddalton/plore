"""Agent 1 — Intent-Driven API Router.

Flow:  optimize_query -> retrieve -> extract_params -> (approval_gate) -> execute
GET/HEAD/OPTIONS are auto-executed; mutating methods pause at a LangGraph interrupt
(the Approval Gate, MAUI guide §6) and resume with the human's decision.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .. import llm
from ..config import config
from .common import (
    candidate_views,
    optimize_query,
    parse_json_object,
    retrieve,
)

_EXTRACT_SYSTEM = (
    "You are a Parameter Extraction Agent for the AWC platform. Given a user request and a "
    "short list of candidate API operations, pick the SINGLE best operation and construct its "
    "call. Respond with ONLY a JSON object of this exact shape:\n"
    '{"service": "...", "method": "...", "path": "...", '
    '"path_params": {}, "query_params": {}, "body": {}}\n'
    "Use the exact method and path of the chosen candidate. Fill path_params for any {placeholders} "
    "in the path. Only include values you can infer from the request; leave unknowns out. No prose."
)


class RouterState(TypedDict, total=False):
    query: str
    optimized_query: str
    candidates: list[dict[str, Any]]
    proposed_call: dict[str, Any]
    approved: bool
    result: dict[str, Any]
    error: str
    response: str  # final natural-language answer for the user


def _node_optimize(state: RouterState) -> RouterState:
    return {"optimized_query": optimize_query(state["query"])}


def _node_retrieve(state: RouterState) -> RouterState:
    cands = retrieve(state["optimized_query"])
    return {"candidates": candidate_views(cands)}


def _node_extract(state: RouterState) -> RouterState:
    ops = state.get("candidates") or []
    if not ops:
        return {"error": "no candidate endpoints found"}
    reply = llm.chat(
        [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": f"User request:\n{state['query']}\n\nCandidate operations:\n"
                + json.dumps(ops, indent=2),
            },
        ],
        max_tokens=512,
    )
    try:
        proposed = parse_json_object(reply)
    except ValueError as exc:
        return {"error": str(exc)}
    proposed["method"] = str(proposed.get("method", "")).upper()
    return {"proposed_call": proposed}


def _node_approval_gate(state: RouterState) -> RouterState:
    # Pauses the run; resume with Command(resume={"approved": true/false}).
    decision = interrupt(
        {
            "type": "approval_required",
            "proposed_call": state.get("proposed_call"),
            "prompt": "Approve this mutating API call? Resume with {'approved': true|false}.",
        }
    )
    approved = decision.get("approved") if isinstance(decision, dict) else bool(decision)
    return {"approved": bool(approved)}


def _node_execute(state: RouterState) -> RouterState:
    call = state.get("proposed_call") or {}
    method = call.get("method", "GET")
    path = call.get("path", "")
    for key, value in (call.get("path_params") or {}).items():
        path = path.replace(f"{{{key}}}", str(value))

    if not config.awc_api_base:
        return {"result": {"status": "dry_run", "would_call": {**call, "resolved_path": path}}}

    url = config.awc_api_base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if config.awc_api_token:
        headers["Authorization"] = f"Bearer {config.awc_api_token}"
    try:
        resp = httpx.request(
            method,
            url,
            params=call.get("query_params") or None,
            json=call.get("body") or None,
            headers=headers,
            timeout=30,
        )
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:2000]
        return {"result": {"status": resp.status_code, "url": url, "method": method, "body": body}}
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the caller
        return {"error": f"execution failed: {exc}"}


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
    return {"response": answer}


def _route_after_extract(state: RouterState) -> str:
    if state.get("error"):
        return "respond"
    method = (state.get("proposed_call") or {}).get("method", "GET")
    return "execute" if method in config.safe_methods else "approval_gate"


def _route_after_gate(state: RouterState) -> str:
    return "execute" if state.get("approved") else "rejected"


def build_graph(checkpointer=None):
    g = StateGraph(RouterState)
    g.add_node("optimize", _node_optimize)
    g.add_node("retrieve", _node_retrieve)
    g.add_node("extract", _node_extract)
    g.add_node("approval_gate", _node_approval_gate)
    g.add_node("execute", _node_execute)
    g.add_node("rejected", _node_rejected)
    g.add_node("respond", _node_respond)

    g.set_entry_point("optimize")
    g.add_edge("optimize", "retrieve")
    g.add_edge("retrieve", "extract")
    g.add_conditional_edges("extract", _route_after_extract,
                            {"execute": "execute", "approval_gate": "approval_gate",
                             "respond": "respond"})
    g.add_conditional_edges("approval_gate", _route_after_gate,
                            {"execute": "execute", "rejected": "rejected"})
    g.add_edge("execute", "respond")
    g.add_edge("rejected", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)


# For the LangGraph server (persistence/interrupts provided by the platform).
graph = build_graph()
