"""Streamlit UI for plore.

Enter a natural-language request; the LangGraph router optimizes the query, retrieves
candidate endpoints from the pgvector registry, extracts parameters, executes
(read-only auto; mutating calls pause for approval here in the UI), and returns a
processed natural-language response. A read-only Discovery mode is also provided.

Graphs run in-process with a per-session checkpointer so the HITL interrupt can be
resumed across Streamlit reruns.

  streamlit run ui/app.py
"""

from __future__ import annotations

import uuid

import streamlit as st
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from plore.config import config
from plore.graphs import discovery, router

st.set_page_config(page_title="plore — AWC API agent", page_icon="🛰️", layout="wide")


@st.cache_resource
def _discovery_graph():
    return discovery.build_graph()


def _router_graph():
    # One checkpointer per browser session so interrupts resume correctly.
    if "router_graph" not in st.session_state:
        st.session_state.saver = MemorySaver()
        st.session_state.router_graph = router.build_graph(checkpointer=st.session_state.saver)
    return st.session_state.router_graph


def _render_trace(state: dict) -> None:
    if state.get("optimized_query"):
        with st.expander("🔎 optimized query"):
            st.code(state["optimized_query"], language="text")
    if state.get("candidates"):
        with st.expander(f"📚 top-{len(state['candidates'])} candidate endpoints"):
            st.json(state["candidates"])
    if state.get("proposed_call"):
        with st.expander("🧩 proposed API call", expanded=True):
            st.json(state["proposed_call"])
    if state.get("result"):
        with st.expander("⚙️ execution result", expanded=True):
            st.json(state["result"])
    if state.get("error"):
        st.error(state["error"])


def _finish_router(state: dict) -> None:
    _render_trace(state)
    if state.get("response"):
        st.chat_message("assistant").write(state["response"])
    st.session_state.pending = None


st.title("🛰️ plore — intent-driven AWC API agent")

with st.sidebar:
    st.subheader("Settings")
    mode = st.radio("Mode", ["Router (execute)", "Discovery (read-only)"])
    st.caption(f"LiteLLM: `{config.litellm_base_url}`")
    st.caption(f"Model: `{config.chat_model}` · embed `{config.embed_model}`")
    st.caption(f"Registry project: `{config.project_id}` · top-k `{config.top_k}`")
    st.caption(
        "Execution: " + ("dry-run (AWC_API_BASE unset)" if not config.awc_api_base
                          else config.awc_api_base)
    )

st.session_state.setdefault("pending", None)

# --- HITL approval gate (rendered when the router interrupted on a mutating call) ---
if st.session_state.pending:
    p = st.session_state.pending
    st.warning("This request resolves to a **mutating** API call and needs your approval.")
    st.json(p["payload"].get("proposed_call", p["payload"]))
    col_yes, col_no = st.columns(2)
    if col_yes.button("✅ Approve", use_container_width=True):
        graph = _router_graph()
        cfg = {"configurable": {"thread_id": p["thread_id"]}}
        state = graph.invoke(Command(resume={"approved": True}), cfg)
        _finish_router(state)
        st.rerun()
    if col_no.button("❌ Reject", use_container_width=True):
        graph = _router_graph()
        cfg = {"configurable": {"thread_id": p["thread_id"]}}
        state = graph.invoke(Command(resume={"approved": False}), cfg)
        _finish_router(state)
        st.rerun()

# --- query input ---
if prompt := st.chat_input("Ask in plain English, e.g. 'list deployed clusters' or 'create a cluster named demo'"):
    st.chat_message("user").write(prompt)

    if mode.startswith("Discovery"):
        with st.spinner("retrieving…"):
            state = _discovery_graph().invoke({"query": prompt})
        st.chat_message("assistant").write(state.get("answer", ""))
        with st.expander("📚 candidates"):
            st.json(state.get("candidates", []))
    else:
        thread_id = str(uuid.uuid4())
        cfg = {"configurable": {"thread_id": thread_id}}
        with st.spinner("optimizing → retrieving → extracting…"):
            state = _router_graph().invoke({"query": prompt}, cfg)
        interrupts = state.get("__interrupt__")
        if interrupts:
            st.session_state.pending = {"thread_id": thread_id, "payload": interrupts[0].value}
            st.rerun()
        else:
            _finish_router(state)
