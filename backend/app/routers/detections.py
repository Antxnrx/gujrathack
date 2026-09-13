"""Features 10–14 exposed as data: detections, tracks, and the ANPR read trail.

Vehicle/person/object detection (10, 13, 14) and tracking (11) are not separate
endpoints — they are the same two tables with a ``class`` filter, which is how the
pipeline actually stores them. Inventing ``/api/vehicles`` and ``/api/persons`` over
one ``detections`` table would be three names for one query and three places for the
filter to drift.

The distinction that does matter is **detection vs track**, because it is the
difference between a frame and a vehicle:

* a ``detection`` is one YOLO box in one frame, with its own crop and its own
  single-frame plate read;
* a ``track`` is ByteTrack's association of those boxes into one object, with the
  consensus plate, the class vote, the colour vote and the path.

Multi-frame ANPR (12) lives on the track: ``plate_text`` there is the consensus over
``plate_read_count`` reads, and ``plate_votes`` keeps the ballot. So
``/tracks/{id}/reads`` returns the whole trail — an operator acting on a plate is
entitled to see how the machine got there.

It also returns a **trust tier**, because a confidence number on its own does not
tell an operator what to do. Measured against plates read by a human
(``scripts/export_plate_labels.py``, nine legible crops): reads above 0.5 were
exact 2/2, reads below 0.3 were exact 0/3, and overall exact match was 33%. The
accuracy is modest; the calibration is the useful part, and ``ml/plate_trust.py``
turns it into act-on / verify / lead-only so every surface says the same thing.

An earlier version of this docstring quoted 17.6% single-read against 85.2%
consensus. Those came from ``scripts/score_anpr.py`` run over *synthetic* tracks,
where the seeder knew the answer — they measured the voting algorithm against a
simulated error model, not ANPR against a road.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, paginate, window
from backend.app.security import Operator, current_operator

# The analytics layer owns the plate vocabulary — what a legal plate is, when one
# looks truncated, and what a confidence band means. The API reads those rules
# rather than restating them, so a retune lands in one place.
from ml import plate_trust
from ml.plate_validator import is_probably_truncated

router = APIRouter(tags=["detections"])


@router.get("/detections")
async def list_detections(
    camera: list[str] | None = Query(default=None),
    detection_class: list[str] | None = Query(default=None, alias="class"),
    color: list[str] | None = Query(default=None),
    plate: str | None = None,
    has_plate: bool | None = None,
    track_uuid: str | None = None,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    span: tuple[datetime | None, datetime | None] = Depends(window),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=50000),
    _: Operator = Depends(current_operator),
) -> dict:
    from_ts, to_ts = span
    rows = await db.fetch(
        """
        SELECT d.id, d.camera_id, d.class, d.confidence, d.bbox, d.plate_text,
               d.plate_confidence, d.dominant_color, d.thumbnail_key, d.ts, d.pts_ms,
               d.track_uuid, d.track_id, d.frame_w, d.frame_h, d.source, d.session_key,
               c.external_id AS camera_external_id, c.name AS camera_name, c.district
        FROM detections d JOIN cameras c ON c.id = d.camera_id
        WHERE ($1::text[] IS NULL OR c.external_id = ANY($1) OR c.id::text = ANY($1))
          AND ($2::text[] IS NULL OR d.class = ANY($2))
          AND ($3::text[] IS NULL OR d.dominant_color = ANY($3))
          AND ($4::text   IS NULL OR d.plate_text = $4)
          AND ($5::boolean IS NULL OR (d.plate_text IS NOT NULL) = $5)
          AND ($6::uuid   IS NULL OR d.track_uuid = $6)
          AND d.confidence >= $7
          AND ($8::timestamptz IS NULL OR d.ts >= $8)
          AND ($9::timestamptz IS NULL OR d.ts <  $9)
        ORDER BY d.ts DESC
        LIMIT $10 OFFSET $11
        """,
        camera, detection_class, color, plate, has_plate,
        as_uuid(track_uuid, "track id") if track_uuid else None,
        min_confidence, from_ts, to_ts, limit + 1, offset,
    )
    body = paginate(rows, limit, offset)
    await storage.attach_thumbnails(body["items"])
    return body


@router.get("/detections/{detection_id}")
async def get_detection(detection_id: str, _: Operator = Depends(current_operator)) -> dict:
    did = as_uuid(detection_id, "detection id")
    row = await db.fetchrow(
        """
        SELECT d.*, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng
        FROM detections d JOIN cameras c ON c.id = d.camera_id WHERE d.id = $1
        """,
        did,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such detection")
    row.pop("embedding", None)          # 512 floats no client needs on a detail view

    track = None
    if row.get("track_uuid"):
        track = await db.fetchrow(
            "SELECT id, class, class_votes, dominant_color, color_votes, plate_text, "
            "plate_confidence, plate_read_count, plate_votes, first_seen, last_seen, "
            "duration_s, detection_count, thumbnail_key, heading_deg, displacement_norm "
            "FROM tracks WHERE id = $1",
            row["track_uuid"],
        )
    # Same camera, ±10 s: the frames either side, for an operator stepping through.
    context = await db.fetch(
        """
        SELECT id, class, plate_text, thumbnail_key, ts, pts_ms, bbox
        FROM detections
        WHERE camera_id = $1 AND id <> $2
          AND ts BETWEEN $3::timestamptz - INTERVAL '10 seconds'
                     AND $3::timestamptz + INTERVAL '10 seconds'
        ORDER BY ts LIMIT 50
        """,
        row["camera_id"], did, row["ts"],
    )
    await storage.attach_thumbnails([row])
    await storage.attach_thumbnails(context)
    if track:
        await storage.attach_thumbnails([track])
    return {"detection": row, "track": track, "context": context}


@router.get("/tracks")
async def list_tracks(
    camera: list[str] | None = Query(default=None),
    track_class: list[str] | None = Query(default=None, alias="class"),
    color: list[str] | None = Query(default=None),
    plate: str | None = None,
    has_plate: bool | None = None,
    min_reads: int = Query(default=0, ge=0, le=100),
    min_duration_s: float = Query(default=0.0, ge=0.0),
    span: tuple[datetime | None, datetime | None] = Depends(window),
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0, le=50000),
    _: Operator = Depends(current_operator),
) -> dict:
    from_ts, to_ts = span
    rows = await db.fetch(
        """
        SELECT t.id, t.camera_id, t.track_id, t.session_key, t.class, t.dominant_color,
               t.plate_text, t.plate_confidence, t.plate_read_count, t.first_seen,
               t.last_seen, t.first_pts_ms, t.last_pts_ms, t.duration_s, t.detection_count,
               t.thumbnail_key, t.heading_deg, t.displacement_norm, t.max_spread_norm,
               t.best_detection_id, t.source,
               c.external_id AS camera_external_id, c.name AS camera_name, c.district
        FROM tracks t JOIN cameras c ON c.id = t.camera_id
        WHERE ($1::text[] IS NULL OR c.external_id = ANY($1) OR c.id::text = ANY($1))
          AND ($2::text[] IS NULL OR t.class = ANY($2))
          AND ($3::text[] IS NULL OR t.dominant_color = ANY($3))
          AND ($4::text   IS NULL OR t.plate_text = $4)
          AND ($5::boolean IS NULL OR (t.plate_text IS NOT NULL) = $5)
          AND COALESCE(t.plate_read_count, 0) >= $6
          AND COALESCE(t.duration_s, 0) >= $7
          AND ($8::timestamptz IS NULL OR t.first_seen >= $8)
          AND ($9::timestamptz IS NULL OR t.first_seen <  $9)
        ORDER BY t.first_seen DESC
        LIMIT $10 OFFSET $11
        """,
        camera, track_class, color, plate, has_plate, min_reads, min_duration_s,
        from_ts, to_ts, limit + 1, offset,
    )
    body = paginate(rows, limit, offset)
    await storage.attach_thumbnails(body["items"])
    return body


@router.get("/tracks/{track_id}")
async def get_track(track_id: str, _: Operator = Depends(current_operator)) -> dict:
    tid = as_uuid(track_id, "track id")
    track = await db.fetchrow(
        """
        SELECT t.*, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng
        FROM tracks t JOIN cameras c ON c.id = t.camera_id WHERE t.id = $1
        """,
        tid,
    )
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such track")
    track.pop("embedding", None)

    detections = await db.fetch(
        "SELECT id, class, confidence, bbox, plate_text, plate_confidence, dominant_color, "
        "thumbnail_key, ts, pts_ms, frame_w, frame_h "
        "FROM detections WHERE track_uuid = $1 ORDER BY ts",
        tid,
    )
    events = await db.fetch(
        "SELECT id, event_type, severity, status, label, started_at, ended_at, metrics "
        "FROM events WHERE track_uuid = $1 ORDER BY started_at",
        tid,
    )
    await storage.attach_thumbnails([track])
    await storage.attach_thumbnails(detections)
    return {"track": track, "detections": detections, "events": events,
            "detection_count": len(detections)}


@router.get("/tracks/{track_id}/reads")
async def plate_reads(track_id: str, _: Operator = Depends(current_operator)) -> dict:
    """Feature 12 — the multi-frame ANPR audit trail for one vehicle.

    Returns every frame-level read alongside the consensus, so the operator can see
    the disagreement rather than a single number. ``plate_votes`` is the per-position
    ballot the consensus was taken from; a position where two characters tie is
    exactly where a plate is most likely wrong, and it is visible here.
    """
    tid = as_uuid(track_id, "track id")
    track = await db.fetchrow(
        "SELECT id, plate_text, plate_confidence, plate_read_count, plate_votes, "
        "class, dominant_color, camera_id FROM tracks WHERE id = $1",
        tid,
    )
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such track")

    reads = await db.fetch(
        "SELECT id, plate_text, plate_confidence, ts, pts_ms, thumbnail_key, bbox "
        "FROM detections WHERE track_uuid = $1 AND plate_text IS NOT NULL ORDER BY ts",
        tid,
    )
    await storage.attach_thumbnails(reads)
    distinct = {}
    for r in reads:
        distinct[r["plate_text"]] = distinct.get(r["plate_text"], 0) + 1
    return {
        "consensus": {
            "plate_text": track["plate_text"],
            "confidence": track["plate_confidence"],
            "read_count": track["plate_read_count"],
            "votes": track["plate_votes"],
            # What to do about it, not just how sure we are. The bands come from
            # a measurement against human-read plates rather than from taste, and
            # they live in one module so every surface says the same thing.
            "trust": plate_trust.describe(
                track["plate_confidence"],
                read_count=track["plate_read_count"],
                truncated=is_probably_truncated(track["plate_text"] or ""),
            ),
        },
        "reads": reads,
        "distinct_reads": sorted(
            ({"plate_text": k, "n": v} for k, v in distinct.items()),
            key=lambda x: -x["n"],
        ),
        "agreement": (
            round(distinct.get(track["plate_text"], 0) / len(reads), 3) if reads else None
        ),
    }
