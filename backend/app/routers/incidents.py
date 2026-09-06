"""Feature 3 — incident timelines.

An incident is the case file: a human-named grouping of events that belong to one
real-world occurrence, across however many cameras and however many hours it took.
The events themselves stay where they are; ``incident_events`` is the join, so the
same event can appear in two incidents (a vehicle involved in both an accident and
a later pursuit) without being copied.

``sequence_no`` is stored rather than derived. The chronological order is available
from ``started_at`` at any time, but an investigator's ordering is not always
chronological — the CCTV clock on one department's cameras may be minutes off, and
the operator reconstructing the sequence knows which frame came first. Storing the
sequence records their judgement; ordering falls back to time when they have not
expressed one.

``ref`` is a human-quotable case number (``INC-2026-0007``) because incidents get
referred to in radio traffic and paperwork, where a UUID is unusable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, audit, paginate
from backend.app.schemas import (IncidentCreate, IncidentEventAdd, IncidentReorder,
                                IncidentUpdate)
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.incidents")
router = APIRouter(prefix="/incidents", tags=["incidents"])

_REF = "'INC-' || to_char(NOW(), 'YYYY') || '-' || lpad(nextval('incident_ref_seq')::text, 4, '0')"


@router.get("")
async def list_incidents(
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    priority: Optional[list[str]] = Query(default=None),
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT i.*,
               COALESCE(s.events, 0)   AS event_count,
               COALESCE(s.cameras, 0)  AS camera_count,
               s.first_event_at, s.last_event_at,
               s.severities,
               (SELECT COUNT(*) FROM notes n
                 WHERE n.target_type = 'incident' AND n.target_id = i.id) AS note_count,
               (SELECT COUNT(*) FROM evidence_snapshots ev WHERE ev.incident_id = i.id) AS snapshot_count
        FROM incidents i
        LEFT JOIN (
            SELECT ie.incident_id,
                   COUNT(*)                    AS events,
                   COUNT(DISTINCT e.camera_id) AS cameras,
                   MIN(e.started_at)           AS first_event_at,
                   MAX(e.started_at)           AS last_event_at,
                   array_agg(DISTINCT e.severity) AS severities
            FROM incident_events ie JOIN events e ON e.id = ie.event_id
            GROUP BY ie.incident_id
        ) s ON s.incident_id = i.id
        WHERE ($1::text[] IS NULL OR i.status   = ANY($1))
          AND ($2::text[] IS NULL OR i.priority = ANY($2))
          AND ($3::text   IS NULL OR i.title ILIKE '%' || $3 || '%'
                                  OR i.ref   ILIKE '%' || $3 || '%'
                                  OR i.description ILIKE '%' || $3 || '%')
        ORDER BY CASE i.status WHEN 'open' THEN 0 WHEN 'investigating' THEN 1 ELSE 2 END,
                 i.updated_at DESC
        LIMIT $4 OFFSET $5
        """,
        status_filter, priority, q, limit + 1, offset,
    )
    return paginate(rows, limit, offset)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_incident(
    body: IncidentCreate,
    operator: Operator = Depends(current_operator),
) -> dict:
    row = await db.fetchrow(
        f"""
        INSERT INTO incidents (ref, title, description, priority, created_by)
        VALUES ({_REF}, $1, $2, $3, $4) RETURNING *
        """,
        body.title, body.description, body.priority, operator.label,
    )
    added = 0
    if body.event_ids:
        added = await _attach(str(row["id"]), body.event_ids, operator, note=None)
    await audit(operator, "incident.create", target=str(row["id"]),
                ref=row["ref"], events=added)
    return {**row, "event_count": added}


@router.get("/{incident_id}")
async def get_incident(incident_id: str, _: Operator = Depends(current_operator)) -> dict:
    iid = as_uuid(incident_id, "incident id")
    incident = await db.fetchrow("SELECT * FROM incidents WHERE id = $1", iid)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such incident")

    timeline = await db.fetch(
        """
        SELECT e.id, e.event_type, e.severity, e.status, e.label, e.started_at, e.ended_at,
               e.pts_ms, e.confidence, e.metrics, e.thumbnail_key, e.plate_text,
               e.track_uuid, e.detection_id, e.source,
               c.id AS camera_id, c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng,
               ie.sequence_no, ie.note AS link_note, ie.added_by, ie.added_at
        FROM incident_events ie
        JOIN events e   ON e.id = ie.event_id
        JOIN cameras c  ON c.id = e.camera_id
        WHERE ie.incident_id = $1
        ORDER BY ie.sequence_no NULLS LAST, e.started_at
        """,
        iid,
    )
    await storage.attach_thumbnails(timeline)

    # The camera hops, in the order the subject actually visited them. This is what
    # feature 9 draws: not a list of cameras but a path through them.
    hops: list[dict] = []
    for row in timeline:
        if hops and hops[-1]["camera_external_id"] == row["camera_external_id"]:
            hops[-1]["until"] = row["started_at"]
            hops[-1]["events"] += 1
            continue
        hops.append({
            "camera_id": str(row["camera_id"]),
            "camera_external_id": row["camera_external_id"],
            "camera_name": row["camera_name"],
            "district": row["district"],
            "lat": row["lat"], "lng": row["lng"],
            "from": row["started_at"], "until": row["started_at"], "events": 1,
        })
    for prev, nxt in zip(hops, hops[1:]):
        nxt["transit_s"] = (nxt["from"] - prev["until"]).total_seconds()

    notes = await db.fetch(
        "SELECT id, body, operator, created_at, updated_at FROM notes "
        "WHERE target_type = 'incident' AND target_id = $1 ORDER BY created_at",
        iid,
    )
    snapshots = await db.fetch(
        "SELECT id, ref, title, operator, item_count, sha256, created_at "
        "FROM evidence_snapshots WHERE incident_id = $1 ORDER BY created_at DESC",
        iid,
    )
    pursuits = await db.fetch(
        "SELECT id, ref, title, status, plate_text, subject_class, subject_color, created_at "
        "FROM pursuits WHERE incident_id = $1 ORDER BY created_at DESC",
        iid,
    )
    return {"incident": incident, "timeline": timeline, "camera_path": hops,
            "notes": notes, "snapshots": snapshots, "pursuits": pursuits}


@router.patch("/{incident_id}")
async def update_incident(
    incident_id: str,
    body: IncidentUpdate,
    operator: Operator = Depends(current_operator),
) -> dict:
    iid = as_uuid(incident_id, "incident id")
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")

    sets, args = [], []
    for column, value in fields.items():
        args.append(value)
        sets.append(f"{column} = ${len(args)}")
    sets.append("updated_at = NOW()")
    if fields.get("status") == "closed":
        sets.append("closed_at = NOW()")
    elif "status" in fields:
        # Reopening must clear the close time, or the case file claims to have been
        # closed while it is open.
        sets.append("closed_at = NULL")

    args.append(iid)
    row = await db.fetchrow(
        f"UPDATE incidents SET {', '.join(sets)} WHERE id = ${len(args)} RETURNING *", *args
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such incident")
    await audit(operator, "incident.update", target=iid, **{k: str(v) for k, v in fields.items()})
    return row


async def _attach(
    incident_id: str,
    event_ids: list[str],
    operator: Operator,
    note: Optional[str],
) -> int:
    """Link events, numbering them after whatever is already in the file."""
    ids = [as_uuid(e, "event id") for e in event_ids]
    base = await db.fetchval(
        "SELECT COALESCE(MAX(sequence_no), 0) FROM incident_events WHERE incident_id = $1",
        incident_id,
    )
    rows = await db.fetch(
        """
        INSERT INTO incident_events (incident_id, event_id, sequence_no, note, added_by)
        SELECT $1, e.id,
               $2 + row_number() OVER (ORDER BY e.started_at),
               $3, $4
        FROM events e WHERE e.id = ANY($5::uuid[])
        ON CONFLICT (incident_id, event_id) DO NOTHING
        RETURNING event_id
        """,
        # `note or ""`, not `note`. The column is `TEXT NOT NULL DEFAULT ''`, and an
        # explicitly bound NULL does not fall back to a default — it violates the
        # constraint. Every incident created *with* events and every add-events call
        # without a note 500'd on this, which is the main path of feature 3.
        incident_id, base, note or "", operator.label, ids,
    )
    await db.execute("UPDATE incidents SET updated_at = NOW() WHERE id = $1", incident_id)
    return len(rows)


@router.post("/{incident_id}/events")
async def add_events(
    incident_id: str,
    body: IncidentEventAdd,
    operator: Operator = Depends(current_operator),
) -> dict:
    iid = as_uuid(incident_id, "incident id")
    if not await db.fetchval("SELECT EXISTS (SELECT 1 FROM incidents WHERE id = $1)", iid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such incident")
    added = await _attach(iid, body.event_ids, operator, body.note)
    await audit(operator, "incident.add_events", target=iid,
                requested=len(body.event_ids), added=added)
    return {"incident_id": iid, "requested": len(body.event_ids), "added": added}


@router.delete("/{incident_id}/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_event(
    incident_id: str,
    event_id: str,
    operator: Operator = Depends(current_operator),
) -> None:
    iid = as_uuid(incident_id, "incident id")
    eid = as_uuid(event_id, "event id")
    await db.execute(
        "DELETE FROM incident_events WHERE incident_id = $1 AND event_id = $2", iid, eid
    )
    await db.execute("UPDATE incidents SET updated_at = NOW() WHERE id = $1", iid)
    await audit(operator, "incident.remove_event", target=iid, event_id=eid)


@router.post("/{incident_id}/reorder")
async def reorder(
    incident_id: str,
    body: IncidentReorder,
    operator: Operator = Depends(current_operator),
) -> dict:
    """Set the investigator's ordering explicitly.

    Any event already in the file and absent from the list keeps its place at the
    end rather than being dropped — a reorder is not a delete, and treating a
    partial list as authoritative would silently shrink the case file.
    """
    iid = as_uuid(incident_id, "incident id")
    if not await db.fetchval("SELECT EXISTS (SELECT 1 FROM incidents WHERE id = $1)", iid):
        # Without this a reorder of a nonexistent incident returned `ordered: N` and
        # changed nothing — the one answer an operator cannot act on.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such incident")
    ordered = [as_uuid(e, "event id") for e in body.event_ids]
    # One statement, not one per event. `WITH ORDINALITY` carries the client's ordering
    # into SQL, which is the only place it can be applied without a round-trip each: the
    # loop this replaces made the request body decide how many round-trips the handler
    # spent, and a 200-event case file paid 200 of them to sort itself.
    await db.execute(
        """
        UPDATE incident_events ie SET sequence_no = pos.ord
        FROM unnest($2::uuid[]) WITH ORDINALITY AS pos(event_id, ord)
        WHERE ie.incident_id = $1 AND ie.event_id = pos.event_id
        """,
        iid, ordered,
    )
    await db.execute(
        """
        UPDATE incident_events ie SET sequence_no = $2 + sub.rn
        FROM (
            SELECT event_id, row_number() OVER (ORDER BY added_at) AS rn
            FROM incident_events
            WHERE incident_id = $1 AND NOT (event_id = ANY($3::uuid[]))
        ) sub
        WHERE ie.incident_id = $1 AND ie.event_id = sub.event_id
        """,
        iid, len(ordered), ordered,
    )
    await db.execute("UPDATE incidents SET updated_at = NOW() WHERE id = $1", iid)
    await audit(operator, "incident.reorder", target=iid, count=len(ordered))
    return {"incident_id": iid, "ordered": len(ordered)}
