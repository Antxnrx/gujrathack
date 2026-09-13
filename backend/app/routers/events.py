"""Features 1, 3 and 8 — the event timeline, tagging, and triage.

``events`` is the unified timeline. Three different producers write to it and the
operator sees one list: ``ml/analytics/rules.py`` for the seven detector rules,
``tr_alert_mirror`` for watchlist and ANPR hits, and this router for what an
operator marks by hand. That is why ``source`` and ``dedupe_key`` exist on the
table — the timeline has to be idempotent under re-runs and still attributable.

Triage is a status transition plus an audit row, not a delete. ``false_positive``
is a first-class status rather than a deletion because the false positives are the
training signal for whether a threshold is set right; deleting them destroys the
only record of what the detector got wrong.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import bus, db, storage
from backend.app.common import as_uuid, audit, paginate, utc, window
from backend.app.schemas import EventCreate, EventStatusUpdate, TagApply, TagCreate
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.events")
router = APIRouter(tags=["events"])


@router.get("/events")
async def list_events(
    event_type: Optional[list[str]] = Query(default=None),
    severity: Optional[list[str]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    camera: Optional[list[str]] = Query(default=None),
    district: Optional[str] = None,
    tag: Optional[list[str]] = Query(default=None),
    plate: Optional[str] = None,
    has_incident: Optional[bool] = None,
    span: tuple[Optional[datetime], Optional[datetime]] = Depends(window),
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0, le=10000),
    order: str = Query(default="recent", pattern="^(recent|oldest|severity)$"),
    _: Operator = Depends(current_operator),
) -> dict:
    from_ts, to_ts = span
    args: list[Any] = [
        event_type, severity, status_filter, camera, district, tag, plate,
        has_incident, from_ts, to_ts,
    ]
    order_sql = {
        "recent": "e.started_at DESC",
        "oldest": "e.started_at ASC",
        # An operator triaging a queue wants the worst thing first, and among
        # equally severe things the newest. Severity is text in the schema, so the
        # ordering is spelled out rather than left to alphabetical accident.
        "severity": ("CASE e.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                     "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, e.started_at DESC"),
    }[order]

    rows = await db.fetch(
        f"""
        SELECT e.id, e.camera_id, e.event_type, e.severity, e.status, e.label,
               e.started_at, e.ended_at, e.pts_ms, e.confidence, e.metrics,
               e.thumbnail_key, e.plate_text, e.track_uuid, e.detection_id,
               e.alert_id, e.zone_id, e.source, e.reviewed_by, e.reviewed_at,
               c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng,
               z.name AS zone_name,
               COALESCE(tg.tags, '[]'::jsonb)  AS tags,
               ie.incident_id,
               inc.ref AS incident_ref,
               -- Notes on the alert count too. An operator triaging the live queue
               -- writes against the alert; the timeline shows the event tr_alert_mirror
               -- made from it. Counting only one of the two makes a note invisible
               -- depending on which door the next operator came in through, which for a
               -- handover is worse than no note at all.
               (SELECT COUNT(*) FROM notes n
                 WHERE (n.target_type = 'event' AND n.target_id = e.id)
                    OR (e.alert_id IS NOT NULL AND n.target_type = 'alert'
                        AND n.target_id = e.alert_id)) AS note_count,
               EXISTS (SELECT 1 FROM bookmarks b
                        WHERE b.target_type = 'event' AND b.target_id = e.id) AS bookmarked
        FROM events e
        JOIN cameras c ON c.id = e.camera_id
        LEFT JOIN zones z ON z.id = e.zone_id
        LEFT JOIN incident_events ie ON ie.event_id = e.id
        LEFT JOIN incidents inc ON inc.id = ie.incident_id
        LEFT JOIN (
            SELECT et.event_id,
                   jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name,
                                                'color', t.color, 'kind', t.kind)) AS tags
            FROM event_tags et JOIN tags t ON t.id = et.tag_id
            GROUP BY et.event_id
        ) tg ON tg.event_id = e.id
        WHERE ($1::text[]  IS NULL OR e.event_type = ANY($1))
          AND ($2::text[]  IS NULL OR e.severity   = ANY($2))
          AND ($3::text[]  IS NULL OR e.status     = ANY($3))
          AND ($4::text[]  IS NULL OR c.external_id = ANY($4) OR e.camera_id::text = ANY($4))
          AND ($5::text    IS NULL OR c.district = $5)
          AND ($6::text[]  IS NULL OR EXISTS (
                  SELECT 1 FROM event_tags et2 JOIN tags t2 ON t2.id = et2.tag_id
                  WHERE et2.event_id = e.id AND t2.name = ANY($6)))
          AND ($7::text    IS NULL OR e.plate_text = $7)
          AND ($8::boolean IS NULL OR (ie.incident_id IS NOT NULL) = $8)
          AND ($9::timestamptz  IS NULL OR e.started_at >= $9)
          AND ($10::timestamptz IS NULL OR e.started_at <  $10)
        ORDER BY {order_sql}
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, limit + 1, offset,
    )
    body = paginate(rows, limit, offset)
    await storage.attach_thumbnails(body["items"])

    storage.attach_plate_trust(body["items"])
    return body


@router.get("/events/summary")
async def summary(
    span: tuple[Optional[datetime], Optional[datetime]] = Depends(window),
    _: Operator = Depends(current_operator),
) -> dict:
    """Counts for the dashboard header, in one query instead of one per tile."""
    from_ts, to_ts = span
    by_type = await db.fetch(
        """
        SELECT event_type, severity, status, COUNT(*) AS n
        FROM events
        WHERE ($1::timestamptz IS NULL OR started_at >= $1)
          AND ($2::timestamptz IS NULL OR started_at <  $2)
        GROUP BY 1, 2, 3
        """,
        from_ts, to_ts,
    )
    totals = await db.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'new')       AS open,
               COUNT(*) FILTER (WHERE severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE status = 'false_positive') AS false_positives,
               COUNT(DISTINCT camera_id)                     AS cameras_involved
        FROM events
        WHERE ($1::timestamptz IS NULL OR started_at >= $1)
          AND ($2::timestamptz IS NULL OR started_at <  $2)
        """,
        from_ts, to_ts,
    )
    hot = await db.fetch(
        """
        SELECT c.external_id, c.name, c.district, COUNT(*) AS events
        FROM events e JOIN cameras c ON c.id = e.camera_id
        WHERE ($1::timestamptz IS NULL OR e.started_at >= $1)
          AND ($2::timestamptz IS NULL OR e.started_at <  $2)
        GROUP BY 1, 2, 3 ORDER BY events DESC LIMIT 10
        """,
        from_ts, to_ts,
    )
    return {"totals": totals, "breakdown": by_type, "busiest_cameras": hot}


@router.get("/events/{event_id}")
async def get_event(event_id: str, _: Operator = Depends(current_operator)) -> dict:
    eid = as_uuid(event_id, "event id")
    event = await db.fetchrow(
        """
        SELECT e.*, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng, c.ingest_url IS NOT NULL AS has_stream,
               z.name AS zone_name, z.polygon AS zone_polygon
        FROM events e JOIN cameras c ON c.id = e.camera_id
        LEFT JOIN zones z ON z.id = e.zone_id
        WHERE e.id = $1
        """,
        eid,
    )
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")

    track = None
    if event.get("track_uuid"):
        track = await db.fetchrow(
            "SELECT id, class, class_votes, dominant_color, color_votes, plate_text, "
            "plate_confidence, plate_read_count, plate_votes, first_seen, last_seen, "
            "duration_s, detection_count, thumbnail_key, path, heading_deg, "
            "displacement_norm, max_spread_norm, best_detection_id "
            "FROM tracks WHERE id = $1",
            event["track_uuid"],
        )

    nearby = await db.fetch(
        """
        SELECT id, event_type, severity, label, started_at, thumbnail_key
        FROM events
        WHERE camera_id = $1 AND id <> $2
          AND started_at BETWEEN $3::timestamptz - INTERVAL '5 minutes'
                             AND $3::timestamptz + INTERVAL '5 minutes'
        ORDER BY started_at
        """,
        event["camera_id"], eid, event["started_at"],
    )
    tags = await db.fetch(
        "SELECT t.id, t.name, t.color, t.kind, et.created_by, et.created_at "
        "FROM event_tags et JOIN tags t ON t.id = et.tag_id WHERE et.event_id = $1 ORDER BY t.name",
        eid,
    )
    # `target_type` is selected, not just filtered on: a note written while triaging
    # the live alert and a note written on the timeline entry are both about this
    # occurrence, but they were written at different moments by operators looking at
    # different things, and flattening that away loses the provenance an investigator
    # uses to weigh them.
    notes = await db.fetch(
        "SELECT id, body, operator, created_at, updated_at, target_type FROM notes "
        "WHERE (target_type = 'event' AND target_id = $1) "
        "   OR ($2::uuid IS NOT NULL AND target_type = 'alert' AND target_id = $2) "
        "ORDER BY created_at",
        eid, event.get("alert_id"),
    )
    incidents = await db.fetch(
        "SELECT i.id, i.ref, i.title, i.status, i.priority, ie.sequence_no, ie.note "
        "FROM incident_events ie JOIN incidents i ON i.id = ie.incident_id WHERE ie.event_id = $1",
        eid,
    )
    await storage.attach_thumbnails([event])
    await storage.attach_thumbnails(nearby)
    if track:
        await storage.attach_thumbnails([track])
    return {"event": event, "track": track, "tags": tags, "notes": notes,
            "incidents": incidents, "nearby_events": nearby}


@router.patch("/events/{event_id}")
async def update_status(
    event_id: str,
    body: EventStatusUpdate,
    operator: Operator = Depends(current_operator),
) -> dict:
    eid = as_uuid(event_id, "event id")
    row = await db.fetchrow(
        """
        UPDATE events SET status = $2, reviewed_by = $3, reviewed_at = NOW()
        WHERE id = $1 RETURNING id, status, reviewed_by, reviewed_at, event_type, camera_id
        """,
        eid, body.status, operator.label,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")
    if body.note:
        await db.execute(
            "INSERT INTO notes (target_type, target_id, body, operator) VALUES ('event', $1, $2, $3)",
            eid, body.note, operator.label,
        )
    await audit(operator, "event.status", target=eid, status=body.status, note=bool(body.note))
    # Push the transition so every other open console updates without polling.
    # On bus.EVENTS, not a topic of its own: Subscription.wants() drops any topic
    # outside bus.CHANNELS, so a bespoke name would be published to nobody. `kind`
    # separates this from the tr_event_notify payload for a newly inserted event.
    bus.broker.publish(bus.EVENTS, {"kind": "status_changed",
                                    "id": str(eid), "status": row["status"],
                                    "camera_id": str(row["camera_id"]),
                                    "reviewed_by": row["reviewed_by"]})
    return row


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreate,
    operator: Operator = Depends(current_operator),
) -> dict:
    """An operator marking something the detectors missed.

    ``source='manual'`` and a dedupe key namespaced to the operator, so a manual
    event is never confused with a rule's output when the analytics pass re-runs.
    """
    cam = await db.fetchrow(
        "SELECT id FROM cameras WHERE external_id = $1 "
        "OR (($1 ~ '^[0-9a-fA-F-]{36}$') AND id = $1::uuid)",
        body.camera_id,
    )
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No camera '{body.camera_id}'")

    started = utc(body.started_at) or datetime.now(tz=timezone.utc)
    row = await db.fetchrow(
        """
        INSERT INTO events (camera_id, event_type, severity, status, label, started_at,
                            pts_ms, detection_id, track_uuid, confidence, metrics,
                            dedupe_key, source)
        VALUES ($1, 'manual', $2, 'new', $3, $4, $5, $6, $7, 1.0, $8::jsonb, $9, 'manual')
        RETURNING *
        """,
        cam["id"], body.severity, body.label, started, body.pts_ms,
        as_uuid(body.detection_id, "detection id") if body.detection_id else None,
        as_uuid(body.track_uuid, "track id") if body.track_uuid else None,
        {**body.metrics, "created_by": operator.subject},
        f"manual:{operator.subject}:{started.isoformat()}:{body.label[:60]}",
    )
    await audit(operator, "event.create", target=str(row["id"]), label=body.label)
    return row


# ── Feature 1: event tagging ────────────────────────────────────────────────
# Tags are a shared vocabulary, not free text per event: an operator filtering for
# "convoy" must find every event anyone tagged that way, which a text column full
# of "Convoy", "convoy?" and "CONVOY " does not deliver. Hence a `tags` table, a
# case-insensitive unique name, and apply-by-name that creates on first use so the
# vocabulary grows from real work instead of an admin screen nobody visits.

@router.get("/tags")
async def list_tags(_: Operator = Depends(current_operator)) -> dict:
    rows = await db.fetch(
        "SELECT t.*, COUNT(et.event_id) AS usage FROM tags t "
        "LEFT JOIN event_tags et ON et.tag_id = t.id GROUP BY t.id ORDER BY usage DESC, t.name"
    )
    return {"tags": rows, "count": len(rows)}


@router.post("/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(body: TagCreate, operator: Operator = Depends(current_operator)) -> dict:
    row = await db.fetchrow(
        """
        INSERT INTO tags (name, color, kind) VALUES (lower($1), $2, $3)
        ON CONFLICT (name) DO UPDATE SET color = EXCLUDED.color RETURNING *
        """,
        body.name.strip(), body.color, body.kind,
    )
    await audit(operator, "tag.create", target=str(row["id"]), name=row["name"])
    return row


@router.post("/events/{event_id}/tags")
async def apply_tags(
    event_id: str,
    body: TagApply,
    operator: Operator = Depends(current_operator),
) -> dict:
    eid = as_uuid(event_id, "event id")
    if not await db.fetchval("SELECT EXISTS (SELECT 1 FROM events WHERE id = $1)", eid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such event")

    applied = []
    for raw in body.tags:
        ref = raw.strip()
        if not ref:
            continue
        tag = await db.fetchrow(
            """
            WITH found AS (
                SELECT id FROM tags
                WHERE lower(name) = lower($1)
                   OR (($1 ~ '^[0-9a-fA-F-]{36}$') AND id = $1::uuid)
            ), made AS (
                INSERT INTO tags (name, kind)
                SELECT lower($1), 'manual' WHERE NOT EXISTS (SELECT 1 FROM found)
                RETURNING id
            )
            SELECT id FROM found UNION ALL SELECT id FROM made
            """,
            ref,
        )
        if tag is None:
            continue
        await db.execute(
            "INSERT INTO event_tags (event_id, tag_id, created_by) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            eid, tag["id"], operator.label,
        )
        applied.append(ref.lower())

    await audit(operator, "event.tag", target=eid, tags=applied)
    rows = await db.fetch(
        "SELECT t.id, t.name, t.color, t.kind FROM event_tags et JOIN tags t ON t.id = et.tag_id "
        "WHERE et.event_id = $1 ORDER BY t.name",
        eid,
    )
    return {"event_id": eid, "tags": rows, "applied": applied}


@router.delete("/events/{event_id}/tags/{tag_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag(
    event_id: str,
    tag_ref: str,
    operator: Operator = Depends(current_operator),
) -> None:
    eid = as_uuid(event_id, "event id")
    await db.execute(
        """
        DELETE FROM event_tags WHERE event_id = $1 AND tag_id IN (
            SELECT id FROM tags WHERE lower(name) = lower($2)
               OR (($2 ~ '^[0-9a-fA-F-]{36}$') AND id = $2::uuid)
        )
        """,
        eid, tag_ref,
    )
    await audit(operator, "event.untag", target=eid, tag=tag_ref)
