"""Feature 8 — alerts.

Two things called "alert" exist in this system and keeping them distinct matters:

* ``alerts`` rows are the **live** signal. ``ml/worker.py`` writes one the moment a
  watchlist plate is read or a vehicle stops where it should not, inside the frame
  loop, with a ``pg_notify`` on ``setu_alerts`` behind it. Latency is the point.
* ``events`` rows are the **timeline**. ``ml/analytics/rules.py`` writes them from a
  completed track, with dwell times, spreads and zone links the live loop cannot
  know yet.

``tr_alert_mirror`` copies watchlist and ANPR alerts into ``events`` so they appear
on the operator's one timeline; the rule-typed alerts are deliberately not mirrored,
because the analytics pass writes a richer event for the same occurrence and two
rows for one stopped lorry is a worse timeline than one.

This router is the live queue view: newest first, unreviewed first, with the
detection and camera context needed to decide without a second request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from backend.app import db, storage
from backend.app.common import as_uuid, audit, paginate, window
from backend.app.security import Operator, current_operator

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertReview(BaseModel):
    status: Literal["confirmed", "dismissed", "pending"]
    note: Optional[str] = None


@router.get("")
async def list_alerts(
    alert_type: Optional[list[str]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    camera: Optional[list[str]] = Query(default=None),
    plate: Optional[str] = None,
    span: tuple[Optional[datetime], Optional[datetime]] = Depends(window),
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    _: Operator = Depends(current_operator),
) -> dict:
    from_ts, to_ts = span
    rows = await db.fetch(
        """
        SELECT a.id, a.alert_type, a.status, a.reviewed_by, a.reviewed_at, a.created_at,
               a.watchlist_entry_id, a.detection_id,
               w.value AS watchlist_value, w.reason AS watchlist_reason, w.type AS watchlist_type,
               d.plate_text, d.plate_confidence, d.class, d.dominant_color, d.bbox,
               d.confidence AS detection_confidence, d.ts, d.pts_ms, d.thumbnail_key,
               d.track_uuid, d.frame_w, d.frame_h,
               c.id AS camera_id, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng,
               e.id AS event_id,
               t.plate_text AS consensus_plate, t.plate_confidence AS consensus_confidence,
               t.plate_read_count
        FROM alerts a
        JOIN detections d ON d.id = a.detection_id
        JOIN cameras c    ON c.id = d.camera_id
        LEFT JOIN watchlist_entries w ON w.id = a.watchlist_entry_id
        LEFT JOIN events e ON e.alert_id = a.id
        LEFT JOIN tracks t ON t.id = d.track_uuid
        WHERE ($1::text[] IS NULL OR a.alert_type = ANY($1))
          AND ($2::text[] IS NULL OR a.status     = ANY($2))
          AND ($3::text[] IS NULL OR c.external_id = ANY($3) OR c.id::text = ANY($3))
          AND ($4::text   IS NULL OR d.plate_text = $4 OR w.value = $4)
          AND ($5::timestamptz IS NULL OR a.created_at >= $5)
          AND ($6::timestamptz IS NULL OR a.created_at <  $6)
        ORDER BY (a.status = 'pending') DESC, a.created_at DESC
        LIMIT $7 OFFSET $8
        """,
        alert_type, status_filter, camera, plate, from_ts, to_ts, limit + 1, offset,
    )
    body = paginate(rows, limit, offset)
    await storage.attach_thumbnails(body["items"])
    return body


@router.get("/summary")
async def summary(_: Operator = Depends(current_operator)) -> dict:
    return {
        "by_type": await db.fetch(
            "SELECT alert_type, status, COUNT(*) AS n FROM alerts GROUP BY 1, 2 ORDER BY 1, 2"
        ),
        "totals": await db.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'pending')   AS pending,
                   COUNT(*) FILTER (WHERE status = 'confirmed') AS confirmed,
                   COUNT(*) FILTER (WHERE status = 'dismissed') AS dismissed,
                   MAX(created_at) AS latest
            FROM alerts
            """
        ),
    }


@router.patch("/{alert_id}")
async def review(
    alert_id: str,
    body: AlertReview,
    operator: Operator = Depends(current_operator),
) -> dict:
    aid = as_uuid(alert_id, "alert id")
    row = await db.fetchrow(
        "UPDATE alerts SET status = $2, reviewed_by = $3, reviewed_at = NOW() "
        "WHERE id = $1 RETURNING *",
        aid, body.status, operator.label,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such alert")

    # Keep the mirrored event's triage state in step. Reviewing the alert and then
    # finding the same thing still 'new' on the timeline is the kind of
    # inconsistency that makes an operator stop trusting the queue.
    mapped = {"confirmed": "acknowledged", "dismissed": "false_positive", "pending": "new"}
    await db.execute(
        "UPDATE events SET status = $2, reviewed_by = $3, reviewed_at = NOW() WHERE alert_id = $1",
        aid, mapped[body.status], operator.label,
    )
    if body.note:
        await db.execute(
            "INSERT INTO notes (target_type, target_id, body, operator) VALUES ('alert', $1, $2, $3)",
            aid, body.note, operator.label,
        )
    await audit(operator, "alert.review", target=aid, status=body.status)
    return row


@router.get("/{alert_id}")
async def get_alert(alert_id: str, _: Operator = Depends(current_operator)) -> dict:
    aid = as_uuid(alert_id, "alert id")
    row = await db.fetchrow(
        """
        SELECT a.*, w.value AS watchlist_value, w.reason AS watchlist_reason,
               d.plate_text, d.plate_confidence, d.class, d.dominant_color, d.bbox,
               d.ts, d.pts_ms, d.thumbnail_key, d.track_uuid, d.frame_w, d.frame_h,
               c.id AS camera_id, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.lat, c.lng, e.id AS event_id
        FROM alerts a
        JOIN detections d ON d.id = a.detection_id
        JOIN cameras c    ON c.id = d.camera_id
        LEFT JOIN watchlist_entries w ON w.id = a.watchlist_entry_id
        LEFT JOIN events e ON e.alert_id = a.id
        WHERE a.id = $1
        """,
        aid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such alert")

    # Every read of the same plate on the same track, so an operator can judge the
    # consensus rather than trust one frame. scripts/score_plate_truth.py measures
    # one frame's read is not an identification — the track's folded plate is.
    reads = []
    if row.get("track_uuid"):
        reads = await db.fetch(
            "SELECT id, plate_text, plate_confidence, ts, pts_ms, thumbnail_key "
            "FROM detections WHERE track_uuid = $1 AND plate_text IS NOT NULL ORDER BY ts",
            row["track_uuid"],
        )
        await storage.attach_thumbnails(reads)
    # Symmetric with routers/events.py: whichever of the two rows an operator wrote
    # against, both views show it. Without this the note an investigator adds on the
    # timeline is invisible to the person working the alert queue, and the two of them
    # are looking at the same vehicle at the same camera at the same second.
    notes = await db.fetch(
        "SELECT id, body, operator, created_at, target_type FROM notes "
        "WHERE (target_type = 'alert' AND target_id = $1) "
        "   OR ($2::uuid IS NOT NULL AND target_type = 'event' AND target_id = $2) "
        "ORDER BY created_at",
        aid, row.get("event_id"),
    )
    await storage.attach_thumbnails([row])
    return {"alert": row, "plate_reads": reads, "notes": notes}
