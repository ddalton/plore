"""Diagnose node — the agentic execute -> diagnose -> retry/HITL loop.

When a call fails (or a deploy is accepted async), this gathers evidence and produces a verdict:
  - SYNC failure (4xx/5xx): interpret the response body. For deployApp, the usual cause is a
    placeholder `appId`; we resolve the real one from the blueprints catalog (name -> appId).
  - ASYNC accept (202): poll the experience/cluster status; a failed deployment carries a
    human-readable `statusReason` (e.g. "Helm install failed: timed out"), optionally deepened
    with live pod logs via the diagnostics API.

The verdict drives routing (set on `diagnosis.route`):
  - "execute"       -> unambiguous, fixable, retryable, budget left: apply fix and auto-retry.
  - "approval_gate" -> ambiguous / needs user input: escalate to HITL with the diagnosis.
  - "respond"       -> nothing to retry (report the outcome / log error to the user).

This implements the MAUI guide's "Intelligent Troubleshooting Agent" / operational use case:
diagnose from logs, auto-remediate when the cause is unambiguous, else escalate to a human.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import awc_api, llm
from ..config import config
from ..obs import get_logger, set_correlation_id
from .common import parse_json_object

_log = get_logger("plore.diagnose")

# Stable AWC console endpoints used for resolution/status (the diagnose step is endpoint-aware).
_BLUEPRINTS = "/api/v0/console/blueprints"
_EXPERIENCES = "/api/v0/console/experiences"
_CLUSTERS = "/api/v0/console/clusters"

_VERDICT_SYSTEM = (
    "You are a Troubleshooting Agent for the AWC platform. Given a user request, the API call that "
    "was made, its failed result, and any gathered evidence, explain the failure and decide whether "
    "it can be fixed and retried. Use ONLY the provided information; do not invent IDs or values. "
    "Respond with ONLY a JSON object of this exact shape:\n"
    '{"cause": "<short cause>", "retryable": <bool>, '
    '"fix": {"path_params": {}, "query_params": {}, "body": {}}, '
    '"needs_user_input": ["<field>", ...], "explanation": "<one or two sentences for the user>"}\n'
    "Set retryable=false for auth/permission errors (401/403) or anything you cannot fix from the "
    "evidence. Put only fields whose correct value you can infer into fix; list fields the human "
    "must supply in needs_user_input. No prose outside the JSON."
)


def _merge_fix(call: dict[str, Any], fix: dict[str, Any]) -> dict[str, Any]:
    """Overlay a fix patch (path_params/query_params/body) onto the proposed call."""
    fix = fix or {}
    return {
        **call,
        "path_params": {**(call.get("path_params") or {}), **(fix.get("path_params") or {})},
        "query_params": {**(call.get("query_params") or {}), **(fix.get("query_params") or {})},
        "body": {**(call.get("body") or {}), **(fix.get("body") or {})},
    }


def _resolve_blueprint(query: str) -> list[dict[str, Any]]:
    """Find blueprints whose name/displayName is mentioned in the user's request, with the newest
    version's appId. Single match => unambiguous fix; 0 or >1 => escalate to the human."""
    res = awc_api.call("GET", _BLUEPRINTS)
    if not awc_api.is_success(res) or not isinstance(res.get("body"), list):
        return []
    q = (query or "").lower()
    matches: list[dict[str, Any]] = []
    for bp in res["body"]:
        if not isinstance(bp, dict):
            continue
        display = str(bp.get("displayName") or "")
        name = str(bp.get("name") or "")
        if (display and display.lower() in q) or (name and name.lower() in q):
            versions = bp.get("versions") or []
            newest = versions[0] if versions and isinstance(versions[0], dict) else {}
            matches.append({
                "blueprintId": bp.get("blueprintId"),
                "displayName": display or name,
                "appId": newest.get("appId"),
                "version": newest.get("version"),
            })
    return [m for m in matches if m.get("appId") is not None]


def _find_by_name(items: Any, name: str, name_keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(items, list) or not name:
        return None
    for it in items:
        if isinstance(it, dict) and any(str(it.get(k)) == name for k in name_keys):
            return it
    return None


def _poll_deploy_status(call: dict[str, Any]) -> dict[str, Any]:
    """For an async (202) deploy, poll experiences/clusters by the names in the request body until
    a terminal status, returning {status, statusReason, target}. Bounded by config so a turn can't
    hang; relies on the API's human-readable statusReason as the primary async diagnosis."""
    body = call.get("body") or {}
    exp_name = body.get("experienceName")
    cluster_name = body.get("clusterName")
    last: dict[str, Any] = {}
    for attempt in range(max(1, config.diagnose_poll_attempts)):
        exp = _find_by_name(awc_api.call("GET", _EXPERIENCES).get("body"),
                            exp_name, ("name",)) if exp_name else None
        target = exp
        if not target and cluster_name:
            target = _find_by_name(awc_api.call("GET", _CLUSTERS).get("body"),
                                   cluster_name, ("name",))
        if isinstance(target, dict):
            status = str(target.get("status") or "")
            last = {"status": status, "statusReason": target.get("statusReason"),
                    "target": {k: target.get(k) for k in ("id", "name", "clusterId",
                                                           "clusterName", "appName")}}
            if status.lower() in ("deployed", "failed", "error"):
                return last
        if attempt < config.diagnose_poll_attempts - 1:
            time.sleep(config.diagnose_poll_interval_s)
    return last or {"status": "pending"}


def _llm_verdict(state: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "user_request": state.get("query"),
        "api_call": state.get("proposed_call"),
        "result": state.get("result"),
        "evidence": evidence,
    }
    try:
        reply = llm.chat(
            [{"role": "system", "content": _VERDICT_SYSTEM},
             {"role": "user", "content": json.dumps(payload, default=str)[:4000]}],
            max_tokens=400,
        )
        return parse_json_object(reply)
    except Exception as exc:  # noqa: BLE001 - a bad verdict must not crash the loop
        _log.warning("verdict parse/chat failed: %s", exc)
        return {"cause": "unknown", "retryable": False, "explanation": "Could not diagnose the failure."}


def diagnose_node(state: dict[str, Any]) -> dict[str, Any]:
    set_correlation_id(state.get("correlation_id"))  # reuse the turn's id on all diagnose probes
    call = state.get("proposed_call") or {}
    result = state.get("result") or {}
    status = result.get("status")
    query = state.get("query", "")
    retries_used = int(state.get("retry_count") or 0)
    budget_left = retries_used < config.max_retries
    path = str(call.get("path") or "")
    evidence: list[dict[str, Any]] = []

    diagnosis: dict[str, Any] = {"cause": "", "explanation": "", "route": "respond",
                                 "fix": {}, "needs_user_input": []}

    # --- ASYNC accept: poll deployment status, report (statusReason is the log-level diagnosis) ---
    if status == 202:
        st = _poll_deploy_status(call)
        evidence.append({"probe": "deploy_status", **st})
        sstate = str(st.get("status") or "pending").lower()
        if sstate in ("failed", "error"):
            # Best-effort: pull the matching diagnostics log lines for this turn. Filter by the
            # correlation id (and namespace); degrades to source='unavailable' without server-side
            # search + a known pod, and never downloads a tar bundle. Must not break the verdict.
            try:
                logs = awc_api.search_logs(correlation_id=state.get("correlation_id"),
                                           log_level="error")
                if logs.get("lines") or logs.get("error"):
                    evidence.append({"probe": "search_logs", **logs})
            except Exception as exc:  # noqa: BLE001 - log search is advisory only
                _log.warning("search_logs probe failed: %s", exc)
            diagnosis.update(cause=st.get("statusReason") or "deployment failed",
                             explanation=f"Deployment {sstate}: {st.get('statusReason') or 'see status'}.")
        else:
            diagnosis.update(cause="accepted",
                             explanation=("Deployment is in progress." if sstate == "pending"
                                          else f"Deployment status: {sstate}."))
        diagnosis["route"] = "respond"  # async status is reported, not auto-retried
        _log.info("diagnose async deploy status=%s reason=%s", sstate, st.get("statusReason"))

    # --- SYNC deployApp failure: resolve blueprint name -> appId ---
    elif path.endswith("/deployApp"):
        matches = _resolve_blueprint(query)
        evidence.append({"probe": "listBlueprints", "matched": matches})
        if len(matches) == 1:
            m = matches[0]
            fix_body = {"appId": m["appId"]}
            if m.get("version"):
                fix_body["version"] = m["version"]
            diagnosis.update(cause=f"appId did not resolve; matched '{m['displayName']}'",
                             fix={"body": fix_body},
                             explanation=(f"Resolved blueprint '{m['displayName']}' to appId "
                                          f"{m['appId']}; retrying the deployment."),
                             route="execute")  # unambiguous -> auto-retry
        elif not matches:
            diagnosis.update(cause="no matching blueprint", needs_user_input=["appId"],
                             route="approval_gate",
                             explanation="No blueprint in the catalog matched your request. "
                             "Specify the blueprint/appId to deploy.")
        else:
            names = ", ".join(f"{m['displayName']} (appId {m['appId']})" for m in matches)
            diagnosis.update(cause="ambiguous blueprint", needs_user_input=["appId"],
                             route="approval_gate",
                             explanation=f"Multiple blueprints matched: {names}. Pick one.")
        _log.info("diagnose deployApp matches=%d route=%s", len(matches), diagnosis["route"])

    # --- generic failure: LLM verdict from the response body + evidence ---
    else:
        verdict = _llm_verdict(state, evidence)
        fix = verdict.get("fix") if isinstance(verdict.get("fix"), dict) else {}
        has_fix = any((fix or {}).get(k) for k in ("path_params", "query_params", "body"))
        needs_input = verdict.get("needs_user_input") or []
        diagnosis.update(cause=verdict.get("cause", ""), needs_user_input=needs_input,
                         explanation=verdict.get("explanation", ""), fix=fix)
        if has_fix and verdict.get("retryable") and not needs_input:
            diagnosis["route"] = "execute"  # unambiguous fix -> auto-retry
        elif needs_input or has_fix:
            diagnosis["route"] = "approval_gate"
        else:
            diagnosis["route"] = "respond"
        _log.info("diagnose generic cause=%r route=%s", diagnosis.get("cause"), diagnosis["route"])

    # Common tail: an auto-retry with no budget left falls back to HITL; apply the fix to the call
    # for any path that will re-execute; count the attempt (bounds both auto and HITL retry loops).
    route = diagnosis["route"]
    if route == "execute" and not budget_left:
        route = diagnosis["route"] = "approval_gate"
    updated_call = _merge_fix(call, diagnosis.get("fix") or {}) if route in ("execute", "approval_gate") else call
    new_retry = retries_used + 1 if route in ("execute", "approval_gate") else retries_used
    return {
        "diagnosis": diagnosis,
        "proposed_call": updated_call,
        "retry_count": new_retry,
        "evidence": evidence,
    }


def route_after_diagnose(state: dict[str, Any]) -> str:
    route = (state.get("diagnosis") or {}).get("route") or "respond"
    # Final budget guard: never auto-retry past the limit even if the verdict asked for it.
    if route == "execute" and int(state.get("retry_count") or 0) > config.max_retries:
        return "respond"
    return route
