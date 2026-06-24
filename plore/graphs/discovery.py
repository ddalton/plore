"""Agent 2 — read-only API Discovery assistant.

Answers "which endpoint do I call to ...?" using retrieval + a grounded explanation.
No parameter extraction, no execution, no approval gate.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .. import llm
from .common import agentic_retrieve, candidate_views

_ANSWER_SYSTEM = (
    "You are an AWC API guide. Given a user question and a list of candidate API operations "
    "(retrieved from the registry), explain which endpoint(s) to call and why, citing the exact "
    "METHOD and path. Be concise. Only use the provided candidates; if none fit, say so."
)


class DiscoveryState(TypedDict, total=False):
    query: str
    candidates: list[dict[str, Any]]
    answer: str


def _node_retrieve(state: DiscoveryState) -> DiscoveryState:
    return {"candidates": candidate_views(agentic_retrieve(state["query"]))}


def _node_answer(state: DiscoveryState) -> DiscoveryState:
    candidates = state.get("candidates") or []
    if not candidates:
        return {"answer": "No matching endpoints found in the registry."}
    answer = llm.chat(
        [
            {"role": "system", "content": _ANSWER_SYSTEM},
            {
                "role": "user",
                "content": f"Question:\n{state['query']}\n\nCandidates:\n"
                + json.dumps(candidates, indent=2),
            },
        ],
        max_tokens=400,
    )
    return {"answer": answer}


def build_graph(checkpointer=None):
    g = StateGraph(DiscoveryState)
    g.add_node("retrieve", _node_retrieve)
    g.add_node("answer", _node_answer)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=checkpointer)


graph = build_graph()
