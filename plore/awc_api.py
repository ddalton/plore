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
from .obs import get_logger

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
    headers = {"Accept": "application/json, */*"}
    token = awc_auth.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Never log headers/token — only method, url, status, and (on failure) the response body.
    _log.info("call %s %s", method, url)
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
