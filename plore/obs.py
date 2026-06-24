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

from .config import config

_configured = False


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
