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

# The plate vocabulary lives in the analytics layer; the API reads it rather than
# restating what a trustworthy read is.
from ml import plate_trust, rto
from ml.plate_validator import is_probably_truncated

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


def attach_plate_trust(rows: list[dict]) -> list[dict]:
    """Tag every row carrying a plate with how far that plate can be trusted.

    Measured on this estate, 89% of consensus plates fall in the band that was
    never once exactly right, 9% in the band that was right a quarter of the
    time, and 2% in the band that was right every time. A list that renders all
    ninety-two identically is therefore telling the operator something false —
    and it is the kind of false that gets acted on, because a plate looks like a
    fact in a way a confidence bar does not.

    The row keeps its ``plate_text`` either way: suppressing a weak read would
    lose a genuine lead, and the operator who can see the crop is better placed
    to judge it than a threshold is. What changes is that the row now says which
    of the three it is.
    """
    for r in rows:
        if not r.get("plate_text"):
            continue
        r["plate_trust"] = plate_trust.describe(
            r.get("plate_confidence"),
            read_count=r.get("plate_read_count"),
            truncated=is_probably_truncated(r["plate_text"]),
        )
        # Provenance the plate already carries. GJ-11 is Junagadh, and at a
        # Junagadh checkpoint the difference between a local registration and
        # one from Kutch is operational information the officer would otherwise
        # have to look up. ``rto_known`` is False for a code Gujarat has never
        # issued (GJ-28, GJ-29), which is a read to distrust rather than a rare
        # vehicle.
        r["plate_district"] = rto.district(r["plate_text"])
        r["plate_origin"] = rto.describe(r["plate_text"])
        r["rto_known"] = rto.is_known_rto(r["plate_text"])
    return rows


def attach_appearance(rows: list[dict]) -> list[dict]:
    """Say how far the colour and class of a vehicle can be trusted.

    Both are CLIP zero-shot over the whole crop, and CLIP on a whole crop is
    measurably unreliable here: it called a black car white on 61 of its 78
    frames, reading the orange cloth on the dashboard through the windscreen.
    Presenting that as "white truck" next to a correctly-read plate is what made
    a working ANPR look broken.

    ``scripts/regrade_appearance.py`` compares each label against an independent
    second opinion and records the disagreements. This surfaces them:

        class_uncertain   two methods named different kinds of vehicle, so the
                          row now reads ``vehicle`` rather than a specific
        colour_uncertain  the bodywork disagreed with the whole-crop reading

    The values are not suppressed — a weak colour is still a search filter, and
    the operator can see the crop. What changes is that the row stops claiming
    to have observed something it inferred.
    """
    for r in rows:
        cv = r.get("class_votes") or {}
        col = r.get("color_votes") or {}
        if isinstance(cv, dict) and cv.get("contested"):
            r["class_uncertain"] = True
            r["class_opinions"] = cv["contested"]
        if isinstance(col, dict) and col.get("contested"):
            r["colour_uncertain"] = True
            r["colour_opinions"] = col["contested"]
    return rows


def health() -> dict:
    try:
        client = _get_client()
        exists = client.bucket_exists(settings.minio_bucket)
        return {"reachable": True, "bucket": settings.minio_bucket, "bucket_exists": exists}
    except Exception as exc:
        return {"reachable": False, "error": type(exc).__name__}
