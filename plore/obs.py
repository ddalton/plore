"""Observability: structured logging to stdout.

plore logs single structured lines to stdout. The cluster's OTel Collector runs a `filelog`
receiver over `/var/log/pods/.../*.log` (container stdout/stderr) and tags each line with
`k8s.namespace.name` / `k8s.pod.name`, so plore's logs land in the AWC diagnostics bundle and are
filterable by namespace. No OTLP SDK is needed in plore — emitting to stdout is sufficient.

Secrets are never logged: the AWC bearer token and the Authorization header are kept out of all
log lines at the call sites (we log method/url/status and response-body snippets, not request
headers).
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

from .config import config

_configured = False

# Correlation id for the current request/turn. Set once per turn (from the session) and read by
# awc_api when stamping outbound calls (X-Request-Id / W3C traceparent) and log lines, so every
# call in a turn — including diagnose-loop probes and retries — shares one id that can be grepped
# across plore's logs and, once downstream services propagate it, the diagnostics bundle.
_correlation_id: ContextVar[str | None] = ContextVar("plore_correlation_id", default=None)


def new_correlation_id(session_id: str | None = None) -> str:
    """Generate a correlation id (a W3C-compatible 32-hex trace id) and make it current."""
    cid = uuid.uuid4().hex  # 32 hex chars == a valid W3C trace-id
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str | None) -> None:
    if cid:
        _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Current correlation id, generating (and setting) an ephemeral one if none is set."""
    cid = _correlation_id.get()
    if not cid:
        cid = new_correlation_id()
    return cid


def traceparent(cid: str | None = None) -> str:
    """A W3C traceparent header value carrying the correlation id as the trace-id."""
    cid = (cid or get_correlation_id())[:32].rjust(32, "0")
    return f"00-{cid}-{uuid.uuid4().hex[:16]}-01"


def configure_logging() -> None:
    """Idempotently configure root logging to emit to stdout at config.log_level. Safe to call
    from every entrypoint (UI, CLI); only the first call installs handlers."""
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=False,
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
