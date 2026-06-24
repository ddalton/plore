"""Shared AWC gateway executor.

One place that builds a request, attaches the bearer token, calls the AWC gateway, logs the
outcome (never the token/headers), and shapes the result. Used by:
  - the router's execute node (the user-intended mutating/GET call), and
  - the diagnose loop, which reuses it for read probes (listExperiences, listDeployedClusters,
    listBlueprints, …) and for the AWC diagnostics API (pod logs / bundle).

Result shape (same as the router built inline before):
  {status, url, method, body|text|artifact, error?}
`status` is an int HTTP code, or "dry_run" when AWC_API_BASE is unset, or "error" on transport
failure (with `error` set).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import httpx

from . import artifacts, awc_auth
from .config import config
from .obs import get_correlation_id, get_logger, traceparent

_log = get_logger("plore.awc_api")


def resolve_path(path: str, path_params: dict[str, Any] | None) -> str:
    """Substitute {placeholders} in a path with provided values."""
    for key, value in (path_params or {}).items():
        path = path.replace(f"{{{key}}}", str(value))
    return path


def call(
    method: str,
    path: str,
    *,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """Execute one AWC gateway call and return a result dict (never raises)."""
    method = (method or "GET").upper()
    path = resolve_path(path, path_params)

    if not config.awc_api_base:
        return {"status": "dry_run", "method": method,
                "would_call": {"method": method, "path": path,
                               "query_params": query_params or {}, "body": body or {}}}

    url = config.awc_api_base.rstrip("/") + path
    # Stamp a correlation id so this call is traceable in logs (and, once downstream services
    # propagate it, across the diagnostics bundle). X-Request-Id is greppable; traceparent is the
    # W3C trace-context form the OTel collector understands.
    cid = get_correlation_id()
    headers = {"Accept": "application/json, */*", "X-Request-Id": cid, "traceparent": traceparent(cid)}
    token = awc_auth.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Never log headers/token — only method, url, status, correlation id, and (on failure) the body.
    _log.info("call %s %s cid=%s", method, url, cid)
    try:
        resp = httpx.request(
            method,
            url,
            params=query_params or None,
            json=body or None,
            headers=headers,
            verify=config.awc_api_verify_tls,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the caller
        _log.error("call %s %s transport error: %s", method, url, exc)
        return {"status": "error", "url": url, "method": method, "error": f"execution failed: {exc}"}

    if resp.status_code >= 400:
        # The line that explains a failed call (e.g. deployApp -> 404 "application not found").
        _log.error("call %s %s -> %s body=%.500s", method, url, resp.status_code, resp.text)
    else:
        _log.info("call %s %s -> %s", method, url, resp.status_code)

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
        result["artifact"] = artifacts.offload(resp.content, key, ctype or "application/octet-stream")
    else:
        result["body"] = resp.text[:2000]
    return result


def _artifact_filename(content_disposition: str, path: str) -> str:
    m = re.search(r'filename\*?=(?:"([^"]+)"|([^;]+))', content_disposition)
    if m:
        return (m.group(1) or m.group(2)).strip().split("/")[-1]
    base = path.rstrip("/").split("/")[-1] or "artifact"
    return base if "." in base else base + ".bin"


def is_success(result: dict[str, Any]) -> bool:
    status = result.get("status")
    return isinstance(status, int) and 200 <= status < 300


# --- AWC diagnostics API helpers (read-only; same gateway + JWT) ----------------------------
# downloadFile reads LIVE pod logs from the k8s API (no OTLP ingestion lag, returns JSON), so it
# is the primary tool for diagnosing a known failed pod. The tar.lz4 bundle (collect→status→
# download) is reserved for broad/multi-pod investigation and is intentionally not auto-fetched.


def pod_logs(pod_name: str, namespace: str | None = None, tail_lines: int = 100) -> dict[str, Any]:
    """GET /api/v1/diagnostics/downloadFile — live pod logs as JSON {found, logs, status}.

    Returns the parsed body on success, or an {error} dict (pod not found / not configured)."""
    ns = namespace or config.diagnostics_namespace
    result = call(
        "GET",
        "/api/v1/diagnostics/downloadFile",
        query_params={"pod_name": pod_name, "namespace": ns, "tail_lines": tail_lines},
    )
    if is_success(result) and isinstance(result.get("body"), dict):
        return result["body"]
    return {"found": False, "podName": pod_name, "namespace": ns,
            "error": result.get("error") or result.get("body") or result.get("status")}


# Proposed server-side log search (see the RFC / JIRA task). Predicate pushdown: the service
# filters + redacts + caps, and returns only matching lines — so the agent never downloads a tar
# bundle into its context just to grep it. Until that endpoint exists, search_logs() degrades to a
# bounded downloadFile (server-tailed) + local grep when a pod is known. It NEVER pulls a full tar.
_SEARCH_LOGS = "/api/v1/diagnostics/searchLogs"


def _grep(lines: str, needles: list[str], limit: int) -> list[str]:
    pats = [n.lower() for n in needles if n]
    out = [ln for ln in (lines or "").splitlines()
           if not pats or any(p in ln.lower() for p in pats)]
    return out[-limit:] if limit and len(out) > limit else out


def search_logs(
    *,
    namespace: str | None = None,
    pod_name: str | None = None,
    label_selector: str | None = None,
    time_range: dict[str, str] | None = None,
    log_level: str | None = None,
    pattern: str | None = None,
    correlation_id: str | None = None,
    tail_lines: int = 500,
    limit: int = 200,
) -> dict[str, Any]:
    """Search diagnostics logs by filter and return only matching lines (never a tar bundle).

    Tries the server-side endpoint first; if it's absent, falls back to a bounded, pod-scoped
    downloadFile + local grep. Without server-side search AND without a pod name there is nothing
    cheap to grep — that case returns source='unavailable' rather than downloading everything."""
    ns = namespace or config.diagnostics_namespace
    body = {k: v for k, v in {
        "namespaceList": [ns] if ns else None,
        "podName": pod_name,
        "labelSelector": label_selector,
        "timeRange": time_range,
        "logLevel": log_level,
        "pattern": pattern,
        "correlationId": correlation_id,
        "limit": limit,
    }.items() if v is not None}

    server = call("POST", _SEARCH_LOGS, body=body)
    if is_success(server):
        return {"source": "server", "lines": server.get("body"), "filter": body}
    server_missing = server.get("status") in (404, 405, 501)

    if pod_name:  # bounded fallback: server-tailed single-pod logs, grepped locally
        pl = pod_logs(pod_name, ns, tail_lines=tail_lines)
        if pl.get("found"):
            lines = _grep(pl.get("logs") or "", [pattern or "", correlation_id or ""], limit)
            return {"source": "downloadFile+grep", "pod": pod_name, "namespace": ns,
                    "lines": lines, "truncated": tail_lines, "server_search": not server_missing}
        return {"source": "downloadFile+grep", "pod": pod_name, "namespace": ns,
                "lines": [], "error": pl.get("error")}

    return {"source": "unavailable", "namespace": ns, "filter": body,
            "error": "server-side searchLogs not available and no pod_name to grep; "
                     "needs the diagnostics searchLogs endpoint (label/namespace/time/correlationId).",
            "server_status": server.get("status")}
