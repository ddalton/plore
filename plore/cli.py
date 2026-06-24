"""Local CLI runners for the graphs (no server needed).

  plore-router "create a cluster named demo"
  plore-discovery "how do I list deployed clusters?"

The router compiles with an in-memory checkpointer so the HITL interrupt can be
resumed from the console.
"""

from __future__ import annotations

import json
import sys

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from .graphs import discovery, router
from .obs import configure_logging

configure_logging()


def _query_from_argv() -> str:
    if len(sys.argv) < 2:
        print("usage: <command> \"<natural language query>\"", file=sys.stderr)
        raise SystemExit(2)
    return " ".join(sys.argv[1:])


def router_main() -> None:
    query = _query_from_argv()
    graph = router.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "cli"}}

    state = graph.invoke({"query": query}, cfg)

    # If we hit the approval gate, LangGraph returns an __interrupt__ payload.
    interrupts = state.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        print(json.dumps(payload, indent=2))
        ans = input("Approve this call? [y/N] ").strip().lower()
        state = graph.invoke(Command(resume={"approved": ans in ("y", "yes")}), cfg)

    print(json.dumps({k: v for k, v in state.items() if not k.startswith("_")}, indent=2, default=str))


def discovery_main() -> None:
    query = _query_from_argv()
    graph = discovery.build_graph()
    state = graph.invoke({"query": query})
    print(state.get("answer", ""))
    print("\n--- candidates ---")
    print(json.dumps(state.get("candidates", []), indent=2, default=str))
