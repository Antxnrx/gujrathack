"""Features 2 and 4 — camera-wise indexing, and the directory the map draws from.

``camera_index`` (migrations/002_backend.sql) is where the per-camera rollup lives:
detection, plate, track, event and zone counts plus class and colour histograms,
computed in the database rather than by fanning out a query per camera from the
API. Thirty cameras today; the same page must not become thirty round-trips when
the deployment has three hundred.

Model 1 owns the authoritative camera directory. This service holds its own
``cameras`` rows because the ML layer's foreign keys point at them and because the
control room has to keep working when Model 1 is unreachable — a directory outage
must not blank the video wall. ``/cameras/sync`` reconciles the two on demand and
reports what changed, rather than a background job silently rewriting the estate.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import audit, envelope, resolve_camera
from backend.app.config import settings
from backend.app.security import Operator, current_operator, require_role

log = logging.getLogger("setu.api.cameras")
router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("")
async def list_cameras(
    district: Optional[str] = None,
    department: Optional[str] = None,
    source_system: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    q: Optional[str] = None,
    with_activity: bool = Query(default=True, description="include rollup counts"),
    _: Operator = Depends(current_operator),
) -> dict:
    """The camera list behind the map and the sidebar.

    ``with_activity=false`` skips ``camera_index`` for the case the UI only needs
    pins and names — the rollup aggregates five tables, and a map redraw should not
    pay for histograms it will not render.
    """
    if with_activity:
        sql = """
            SELECT ci.*, c.ingest_url IS NOT NULL AS has_stream,
                   c.department, c.camera_uid, c.metadata
            FROM camera_index ci JOIN cameras c ON c.id = ci.id
            WHERE ($1::text IS NULL OR ci.district = $1)
              AND ($2::text IS NULL OR c.department = $2)
              AND ($3::text IS NULL OR c.metadata->>'source_system' = $3)
              AND ($4::text IS NULL OR ci.status = $4)
              AND ($5::text IS NULL OR ci.name ILIKE '%' || $5 || '%'
                                    OR ci.external_id ILIKE '%' || $5 || '%'
                                    OR ci.area ILIKE '%' || $5 || '%')
            ORDER BY ci.district NULLS LAST, ci.name
        """
    else:
        sql = """
            SELECT c.id, c.external_id, c.name, c.district, c.area, c.lat, c.lng,
                   c.status, c.department, c.camera_uid, c.geocode_precision,
                   c.ingest_url IS NOT NULL AS has_stream, c.metadata
            FROM cameras c
            WHERE ($1::text IS NULL OR c.district = $1)
              AND ($2::text IS NULL OR c.department = $2)
              AND ($3::text IS NULL OR c.metadata->>'source_system' = $3)
              AND ($4::text IS NULL OR c.status = $4)
              AND ($5::text IS NULL OR c.name ILIKE '%' || $5 || '%'
                                    OR c.external_id ILIKE '%' || $5 || '%'
                                    OR c.area ILIKE '%' || $5 || '%')
            ORDER BY c.district NULLS LAST, c.name
        """
    rows = await db.fetch(sql, district, department, source_system, status_filter, q)
    return envelope(rows, limit=len(rows), offset=0, total=len(rows))


@router.get("/systems")
async def systems(_: Operator = Depends(current_operator)) -> dict:
    """The estates this deployment federates, and how many cameras each contributes.

    This is the Model 2 deliverable made checkable: "connected to sample feeds from
    at least two different systems" should be answerable from the running service,
    not from a slide. ``source_system`` names whose cameras they are; ``protocols``
    names how this deployment reaches them.
    """
    rows = await db.fetch(
        """
        SELECT COALESCE(metadata->>'source_system', department, 'unknown') AS source_system,
               department,
               COUNT(*)                                          AS cameras,
               COUNT(*) FILTER (WHERE status = 'active')          AS active,
               COUNT(DISTINCT district)                           AS districts,
               -- COALESCE, because array_agg FILTER returns NULL rather than an
               -- empty array when no row passes the filter. Cameras imported
               -- before their protocol is known produce exactly that, and a null
               -- where the client is promised a list is a crash in the client,
               -- not a missing value it can render.
               COALESCE(
                   array_agg(DISTINCT metadata->>'stream_protocol')
                       FILTER (WHERE metadata->>'stream_protocol' IS NOT NULL),
                   '{}'
               )                                                  AS protocols,
               MIN(camera_uid)                                    AS example_uid
        FROM cameras
        GROUP BY 1, 2
        ORDER BY cameras DESC
        """
    )
    return {"systems": rows, "count": len(rows)}


@router.get("/districts")
async def districts(_: Operator = Depends(current_operator)) -> dict:
    rows = await db.fetch(
        """
        SELECT district, COUNT(*) AS cameras,
               COUNT(*) FILTER (WHERE status = 'active') AS active,
               array_agg(DISTINCT area) FILTER (WHERE area IS NOT NULL) AS areas,
               AVG(lat) AS lat, AVG(lng) AS lng
        FROM cameras WHERE district IS NOT NULL
        GROUP BY district ORDER BY district
        """
    )
    return {"districts": rows, "count": len(rows)}


@router.get("/{ref}")
async def get_camera(ref: str, _: Operator = Depends(current_operator)) -> dict:
    cam = await resolve_camera(ref)
    # The list endpoint derives this and the detail did not, so a camera page
    # reported "no relay" next to a player that was streaming from it. Derived
    # here rather than in the UI: one definition, both endpoints.
    cam = {**cam, "has_stream": cam.get("ingest_url") is not None}
    rollup = await db.fetchrow("SELECT * FROM camera_index WHERE id = $1", cam["id"])
    zones = await db.fetch(
        "SELECT id, name, kind, polygon, direction_deg, tolerance_deg, classes, active "
        "FROM zones WHERE camera_id = $1 ORDER BY name",
        cam["id"],
    )
    neighbours = await db.fetch(
        """
        SELECT l.dst_camera_id AS camera_id, c.external_id, c.name, c.district,
               l.median_transit_s, l.p10_transit_s, l.p90_transit_s,
               l.distance_m, l.observations, l.probability, l.evidence
        FROM camera_links l JOIN cameras c ON c.id = l.dst_camera_id
        WHERE l.src_camera_id = $1
        ORDER BY l.probability DESC NULLS LAST, l.median_transit_s
        """,
        cam["id"],
    )
    recent = await db.fetch(
        """
        SELECT id, event_type, severity, status, label, started_at, thumbnail_key, plate_text
        FROM events WHERE camera_id = $1 ORDER BY started_at DESC LIMIT 10
        """,
        cam["id"],
    )
    await storage.attach_thumbnails(recent)
    return {
        "camera": cam,
        "activity": rollup,
        "zones": zones,
        "neighbours": neighbours,
        "recent_events": recent,
    }


@router.get("/{ref}/timeline")
async def camera_timeline(
    ref: str,
    bucket_minutes: int = Query(default=15, ge=1, le=1440),
    hours: int = Query(default=24, ge=1, le=24 * 30),
    _: Operator = Depends(current_operator),
) -> dict:
    """Activity histogram for the scrub bar — feature 3's per-camera strip.

    Bucketed in SQL with ``date_bin`` so the response is a few hundred rows
    regardless of how many detections it summarises. Sending 52k detection
    timestamps to the browser to be counted there is how a timeline becomes the
    slowest thing in the UI.
    """
    cam = await resolve_camera(ref)
    buckets = await db.fetch(
        """
        SELECT date_bin(make_interval(mins => $2), ts, TIMESTAMPTZ 'epoch') AS bucket,
               COUNT(*) AS detections,
               COUNT(plate_text) AS plates,
               COUNT(DISTINCT track_uuid) AS tracks
        FROM detections
        WHERE camera_id = $1 AND ts > NOW() - make_interval(hours => $3)
        GROUP BY 1 ORDER BY 1
        """,
        cam["id"], bucket_minutes, hours,
    )
    events = await db.fetch(
        """
        SELECT id, event_type, severity, status, label, started_at, ended_at, pts_ms, confidence
        FROM events
        WHERE camera_id = $1 AND started_at > NOW() - make_interval(hours => $2)
        ORDER BY started_at
        """,
        cam["id"], hours,
    )
    return {
        "camera_id": str(cam["id"]),
        "external_id": cam["external_id"],
        "bucket_minutes": bucket_minutes,
        "buckets": buckets,
        "events": events,
    }


# Above this share of the active estate, a single sync is refused rather than applied.
# A quarter is chosen to sit clear of ordinary churn — a department retiring three of
# thirty cameras is a Tuesday — while still catching the case that matters, which is a
# directory that turns out to describe somewhere else entirely.
_BULK_RETIRE_SHARE = 0.25


@router.post("/sync", dependencies=[Depends(require_role("admin"))])
async def sync_from_model1(
    allow_bulk_retire: bool = Query(
        default=False,
        description="permit a sync that retires more than a quarter of the active estate",
    ),
    operator: Operator = Depends(current_operator),
) -> dict:
    """Reconcile the local camera table against Model 1's directory.

    Upsert-only. Cameras Model 1 no longer lists are marked ``retired`` rather than
    deleted, because ``detections.camera_id`` references them — deleting a camera
    would cascade away every detection ever recorded on it, which is precisely the
    evidence a control room exists to keep.
    """
    if not settings.model1_base_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MODEL1_BASE_URL is not configured")

    url = settings.model1_base_url.rstrip("/") + "/api/cameras"
    headers = {"X-API-Key": settings.model1_api_key} if settings.model1_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Model 1 camera directory unreachable: {type(exc).__name__}"
        )

    # Key presence, not truthiness. `payload.get("items") or payload.get("cameras") or
    # payload` falls straight through an *empty* items list — [] is falsy — and ends up
    # handing the envelope dict to the isinstance check, so a department that currently
    # publishes no cameras was reported as "Model 1 returned an unexpected shape". An
    # empty directory is a valid answer, and the `if seen` guard below is written to
    # handle it; it just never got the chance.
    if isinstance(payload, dict):
        for key in ("items", "cameras"):
            if isinstance(payload.get(key), list):
                listed = payload[key]
                break
        else:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                "Model 1 returned an unexpected shape: no 'items' or 'cameras' list")
    elif isinstance(payload, list):
        listed = payload
    else:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Model 1 returned an unexpected shape")

    # Is this the whole directory, or one page of it? It decides whether the retirement
    # pass below is safe to run at all: retiring "everything not in this list" is only
    # correct if the list is complete, and Model 1's directory is an `items` + `total`
    # envelope — the same convention this API uses for its own listings. A first page of
    # 25 against a total of 30 would retire the other 5 perfectly good cameras, and a
    # retired camera drops out of the transition graph, the geo pass and every camera
    # picker, so the network quietly loses corridors nobody removed.
    total = payload.get("total") if isinstance(payload, dict) else None
    complete = not (isinstance(total, int) and total > len(listed))

    seen, created, updated = [], 0, 0
    for c in listed:
        external_id = str(c.get("external_id") or c.get("camera_id") or c.get("id") or "").strip()
        if not external_id:
            continue
        seen.append(external_id)
        result = await db.fetchrow(
            """
            INSERT INTO cameras (external_id, name, lat, lng, ingest_url, status,
                                 district, area, department, camera_uid, geocode_precision, metadata)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, 'active'), $7, $8, $9, $10,
                    COALESCE($11, 'exact'), COALESCE($12::jsonb, '{}'::jsonb))
            ON CONFLICT (external_id) DO UPDATE SET
                name       = COALESCE(EXCLUDED.name, cameras.name),
                lat        = COALESCE(EXCLUDED.lat, cameras.lat),
                lng        = COALESCE(EXCLUDED.lng, cameras.lng),
                ingest_url = COALESCE(EXCLUDED.ingest_url, cameras.ingest_url),
                status     = COALESCE(EXCLUDED.status, cameras.status),
                district   = COALESCE(EXCLUDED.district, cameras.district),
                area       = COALESCE(EXCLUDED.area, cameras.area),
                department = COALESCE(EXCLUDED.department, cameras.department),
                camera_uid = COALESCE(cameras.camera_uid, EXCLUDED.camera_uid),
                metadata   = cameras.metadata || EXCLUDED.metadata
            RETURNING (xmax = 0) AS inserted
            """,
            external_id,
            c.get("name"), c.get("lat"), c.get("lng"), c.get("ingest_url") or c.get("stream_url"),
            c.get("status"), c.get("district"), c.get("area"), c.get("department"),
            c.get("camera_uid"), c.get("geocode_precision"),
            {
                k: v for k, v in c.items()
                if k not in {"external_id", "camera_id", "id", "name", "lat", "lng",
                             "ingest_url", "stream_url", "status", "district", "area",
                             "department", "camera_uid", "geocode_precision"}
            },
        )
        if result and result["inserted"]:
            created += 1
        else:
            updated += 1

    # `if seen` guards the other direction: an empty or unparseable directory must not be
    # read as "the department decommissioned every camera". Upserts are safe on a partial
    # page — a camera described twice is a camera described once — so only this pass is
    # skipped, and the response says it was skipped rather than reporting `retired: 0`,
    # which a caller cannot tell apart from "nothing needed retiring".
    retired, refused = 0, None
    if seen and complete:
        # Counted before it is applied. A sync pointed at the wrong host — a stand-in, a
        # staging directory, a typo'd MODEL1_BASE_URL — answers with a small, internally
        # consistent directory that passes every check above, and this pass then retires
        # the real estate in one statement. Nothing is deleted, so it is recoverable, but
        # a retired camera vanishes from the transition graph, the geo pass and every
        # picker, which for a control room mid-shift is indistinguishable from an outage.
        # A genuine decommission of that size is a deliberate act and gets an explicit
        # flag, the same way the reset paths in scripts/ do.
        doomed = await db.fetch(
            "SELECT external_id FROM cameras "
            "WHERE external_id <> ALL($1::text[]) AND status <> 'retired'",
            seen,
        )
        active_now = await db.fetchval("SELECT COUNT(*) FROM cameras WHERE status <> 'retired'")
        share = len(doomed) / max(active_now, 1)
        if doomed and share > _BULK_RETIRE_SHARE and not allow_bulk_retire:
            refused = (f"would retire {len(doomed)} of {active_now} active cameras "
                       f"({share:.0%}); refused. Re-run with allow_bulk_retire=true if "
                       f"the directory at {url} is authoritative.")
            log.warning("cameras.sync refused bulk retirement: %s", refused)
        elif doomed:
            retired = len(await db.fetch(
                "UPDATE cameras SET status = 'retired' "
                "WHERE external_id <> ALL($1::text[]) AND status <> 'retired' RETURNING id",
                seen,
            ))

    partial = None if complete else (
        f"directory reported total={total} but returned {len(listed)} — retiring on a "
        f"partial listing would remove cameras Model 1 still has")
    await audit(operator, "cameras.sync", target=url, created=created, updated=updated,
                retired=retired, partial_listing=not complete, refused=refused)
    return {"source": url, "listed": len(seen), "created": created, "updated": updated,
            "retired": retired, "retirement_skipped": partial or refused}
