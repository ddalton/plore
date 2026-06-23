"""Artifact offload to S3/MinIO.

Binary API responses (e.g. diagnostic bundles) are streamed to object storage and
referenced by key + presigned URL, so they persist with the session instead of
bloating the graph state with raw bytes.
"""

from __future__ import annotations

import io
from datetime import timedelta

from .config import config

_client = None


def enabled() -> bool:
    return bool(config.minio_endpoint)


def _client_or_none():
    global _client
    if not enabled():
        return None
    if _client is None:
        from minio import Minio

        _client = Minio(
            config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        )
    return _client


def offload(data: bytes, key: str, content_type: str) -> dict:
    """Store `data` under `key` in the bucket; return a reference (no raw bytes)."""
    client = _client_or_none()
    if client is None:
        return {"stored": False, "reason": "object storage not configured"}

    bucket = config.minio_bucket
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(
        bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type or "application/octet-stream",
    )
    url = None
    try:
        url = client.presigned_get_object(bucket, key, expires=timedelta(days=7))
    except Exception:  # noqa: BLE001 - presign is best-effort; the key still identifies it
        pass
    return {
        "stored": True,
        "bucket": bucket,
        "key": key,
        "size": len(data),
        "content_type": content_type,
        "url": url,
    }
