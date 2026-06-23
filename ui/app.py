"""Streamlit UI for plore.

Enter a natural-language request; the LangGraph router optimizes the query, retrieves
candidate endpoints from the pgvector registry, extracts parameters, executes
(read-only auto; mutating calls pause for approval here in the UI), and returns a
processed natural-language response. A read-only Discovery mode is also provided.

Sessions are durable: the router runs against a Postgres-backed LangGraph checkpointer
(reusing the pgvector database), so a session's state + interaction log survive restarts
and can be resumed by its Session ID. The HITL interrupt also resumes from there.

  streamlit run ui/app.py
"""

from __future__ import annotations

import uuid

import streamlit as st
from langgraph.types import Command

from plore.checkpoint import get_checkpointer
from plore.config import config
from plore.graphs import discovery, router

st.set_page_config(page_title="plore — AWC API agent", page_icon="🛰️", layout="wide")


@st.cache_resource
def _discovery_graph():
    return discovery.build_graph()


@st.cache_resource
def _router_graph():
    # Durable Postgres checkpointer, shared across sessions; threads isolate sessions.
    return router.build_graph(checkpointer=get_checkpointer())


def _cfg():
    return {"configurable": {"thread_id": st.session_state.session_id}}


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

st.session_state.setdefault("session_id", str(uuid.uuid4()))
st.session_state.setdefault("pending", None)

with st.sidebar:
    st.subheader("Settings")
    mode = st.radio("Mode", ["Router (execute)", "Discovery (read-only)"])
    st.divider()
    st.subheader("Session (durable)")
    sid = st.text_input("Session ID", value=st.session_state.session_id,
                        help="Resume a past session by pasting its ID.")
    if sid != st.session_state.session_id:
        st.session_state.session_id = sid
        st.session_state.pending = None
        st.rerun()
    if st.button("🆕 New session", use_container_width=True):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.pending = None
        st.rerun()
    st.divider()
    st.caption(f"LiteLLM: `{config.litellm_base_url}`")
    st.caption(f"Model: `{config.chat_model}` · embed `{config.embed_model}`")
    st.caption(f"Registry project: `{config.project_id}` · top-k `{config.top_k}`")
    st.caption(
        "Execution: " + ("dry-run (AWC_API_BASE unset)" if not config.awc_api_base
                          else config.awc_api_base)
    )

# --- HITL approval gate (rendered when the router interrupted on a mutating call) ---
if st.session_state.pending:
    p = st.session_state.pending
    st.warning("This request resolves to a **mutating** API call and needs your approval.")
    st.json(p["payload"].get("proposed_call", p["payload"]))
    col_yes, col_no = st.columns(2)
    if col_yes.button("✅ Approve", use_container_width=True):
        state = _router_graph().invoke(Command(resume={"approved": True}), _cfg())
        _finish_router(state)
        st.rerun()
    if col_no.button("❌ Reject", use_container_width=True):
        state = _router_graph().invoke(Command(resume={"approved": False}), _cfg())
        _finish_router(state)
        st.rerun()

# --- query input ---
if prompt := st.chat_input("Ask in plain English, e.g. 'list deployed clusters' or 'create a cluster named demo'"):
    st.chat_message("user").write(prompt)
    if st.session_state.pending:
        st.warning("Resolve the pending approval above before sending a new request.")
    elif mode.startswith("Discovery"):
        with st.spinner("retrieving…"):
            state = _discovery_graph().invoke({"query": prompt})
        st.chat_message("assistant").write(state.get("answer", ""))
        with st.expander("📚 candidates"):
            st.json(state.get("candidates", []))
    else:
        with st.spinner("optimizing → retrieving → extracting…"):
            state = _router_graph().invoke({"query": prompt}, _cfg())
        interrupts = state.get("__interrupt__")
        if interrupts:
            st.session_state.pending = {"payload": interrupts[0].value}
            st.rerun()
        else:
            _finish_router(state)

# --- durable session history (persisted via the Postgres checkpointer) ---
if not mode.startswith("Discovery"):
    try:
        snap = _router_graph().get_state(_cfg())
        log = (snap.values or {}).get("session_log", []) if snap else []
    except Exception:  # noqa: BLE001 - history view must never break the page
        log = []
    with st.expander(f"🗂 session history · `{st.session_state.session_id}` · {len(log)} turn(s)"):
        if not log:
            st.caption("No turns yet in this session.")
        for i, entry in enumerate(log, 1):
            st.markdown(f"**{i}. {entry.get('query', '')}**")
            if entry.get("proposed_call"):
                st.code(f"{entry['proposed_call'].get('method','')} {entry['proposed_call'].get('path','')}")
            st.write(entry.get("response", ""))
