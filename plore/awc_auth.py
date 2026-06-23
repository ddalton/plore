"""AWC bearer-token acquisition for executing API calls through the gateway.

Knox enforces auth at the gateway, so calls need a JWT. Resolution order:
  1. AWC_API_TOKEN — a ready bearer token, used as-is.
  2. AWC_ACCESS_KEY_ID/SECRET — exchanged (client_credentials) for a JWT at the
     Knox-exempt path POST /api/v0/auth/access-keys/token, then cached until expiry.
  3. otherwise None (caller falls back to dry-run / unauthenticated).
"""

from __future__ import annotations

import threading
import time

import httpx

from .config import config

_lock = threading.Lock()
_cache: dict[str, float | str | None] = {"token": None, "exp": 0.0}


def get_token() -> str | None:
    if config.awc_api_token:
        return config.awc_api_token
    if not (config.awc_access_key_id and config.awc_access_key_secret and config.awc_api_base):
        return None

    now = time.time()
    with _lock:
        if _cache["token"] and now < float(_cache["exp"]) - 30:
            return str(_cache["token"])
        url = config.awc_api_base.rstrip("/") + "/api/v0/auth/access-keys/token"
        resp = httpx.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(config.awc_access_key_id, config.awc_access_key_secret),
            verify=config.awc_api_verify_tls,
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        _cache["token"] = token
        _cache["exp"] = now + float(body.get("expires_in", 3600))
        return token
