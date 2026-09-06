"""Presigned URLs for thumbnails and evidence objects.

``ml/storage.py`` writes thumbnails and returns an object key. It has no read
helper, because the inference layer never reads them back — which means the API
is the first thing that needs one.

Presigned rather than proxied, deliberately. A control-room timeline shows
hundreds of thumbnails at once; relaying each one through this process would put
every byte of every image through the API's event loop and make the service's
throughput a function of how many pictures an operator is looking at. A presigned
URL is a short-lived capability the browser redeems against MinIO directly.

The trade-off is that the URL is a bearer capability: anyone holding it can fetch
that object until it expires. That is why the expiry is minutes, not days, and why
the URL is minted per response rather than stored anywhere.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from backend.app.config import settings

log = logging.getLogger("setu.api.storage")

_client = None
_unavailable_logged = False


def _get_client():
    global _client
    if _client is None:
        from minio import Minio
        _client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _client


def _presign_sync(key: str) -> Optional[str]:
    global _unavailable_logged
    try:
        return _get_client().presigned_get_object(
            settings.minio_bucket, key,
            expires=timedelta(seconds=settings.presign_expiry_seconds),
        )
    except Exception as exc:
        # A missing MinIO must not fail a search. The row is still the answer;
        # the picture is an enrichment, and returning null lets the UI render a
        # placeholder instead of showing the operator an error page.
        if not _unavailable_logged:
            log.warning("presign failed (%s) — thumbnails will be null", exc)
            _unavailable_logged = True
        return None


async def presign(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    return await asyncio.to_thread(_presign_sync, key)


async def presign_many(keys: list[Optional[str]]) -> dict[str, Optional[str]]:
    """Sign a batch in one thread hop.

    Signing is pure local HMAC — no network — so a hundred keys cost microseconds
    each. Doing them in one ``to_thread`` rather than a hundred avoids paying the
    executor hand-off per thumbnail on a page of search results.
    """
    unique = [k for k in dict.fromkeys(keys) if k]
    if not unique:
        return {}

    def _batch() -> dict[str, Optional[str]]:
        return {k: _presign_sync(k) for k in unique}

    return await asyncio.to_thread(_batch)


async def attach_thumbnails(rows: list[dict], key_field: str = "thumbnail_key") -> list[dict]:
    """Add a ``thumbnail_url`` to each row that has a key. Mutates and returns rows."""
    urls = await presign_many([r.get(key_field) for r in rows])
    for r in rows:
        r["thumbnail_url"] = urls.get(r.get(key_field)) if r.get(key_field) else None
    return rows


def health() -> dict:
    try:
        client = _get_client()
        exists = client.bucket_exists(settings.minio_bucket)
        return {"reachable": True, "bucket": settings.minio_bucket, "bucket_exists": exists}
    except Exception as exc:
        return {"reachable": False, "error": type(exc).__name__}
