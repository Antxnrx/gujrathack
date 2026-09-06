"""Feature 6 — event bookmarking.

Per-operator, not global: the unique constraint is
``(target_type, target_id, operator)``, so two investigators working the same night
keep separate shortlists. A shared "starred" list would mean one operator's triage
queue emptying because a colleague cleared theirs.

Bookmarks point at anything with a UUID — events, detections, tracks, cameras,
incidents, pursuits — rather than events alone. In practice the thing an operator
wants to come back to is often the *vehicle* (a track) or the *camera*, not the
alert that drew their eye to it, and a bookmark table that only accepts events
forces them to bookmark the wrong object.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, audit, paginate
from backend.app.schemas import BookmarkCreate
from backend.app.security import Operator, current_operator

router = APIRouter(prefix="/bookmarks", tags=["bookmarks"])

# What to show for each bookmarkable type, so a bookmark list renders without the
# UI making one request per row. Kept as SQL fragments rather than a generic join
# because each type's "what is this" is a different set of columns.
_RESOLVERS: dict[str, str] = {
    "event": """
        SELECT e.id, e.label AS title, e.event_type AS subtitle, e.severity,
               e.started_at AS ts, e.thumbnail_key, c.external_id AS camera_external_id,
               c.name AS camera_name
        FROM events e JOIN cameras c ON c.id = e.camera_id WHERE e.id = ANY($1::uuid[])
    """,
    "detection": """
        SELECT d.id, COALESCE(d.plate_text, d.class) AS title, d.class AS subtitle,
               NULL::text AS severity, d.ts, d.thumbnail_key,
               c.external_id AS camera_external_id, c.name AS camera_name
        FROM detections d JOIN cameras c ON c.id = d.camera_id WHERE d.id = ANY($1::uuid[])
    """,
    "track": """
        SELECT t.id, COALESCE(t.plate_text, t.class) AS title,
               concat_ws(' ', t.dominant_color, t.class) AS subtitle,
               NULL::text AS severity, t.first_seen AS ts, t.thumbnail_key,
               c.external_id AS camera_external_id, c.name AS camera_name
        FROM tracks t JOIN cameras c ON c.id = t.camera_id WHERE t.id = ANY($1::uuid[])
    """,
    "camera": """
        SELECT c.id, c.name AS title, c.district AS subtitle, NULL::text AS severity,
               NULL::timestamptz AS ts, NULL::text AS thumbnail_key,
               c.external_id AS camera_external_id, c.name AS camera_name
        FROM cameras c WHERE c.id = ANY($1::uuid[])
    """,
    "incident": """
        SELECT i.id, i.title, i.ref AS subtitle, i.priority AS severity,
               i.created_at AS ts, NULL::text AS thumbnail_key,
               NULL::text AS camera_external_id, NULL::text AS camera_name
        FROM incidents i WHERE i.id = ANY($1::uuid[])
    """,
    "pursuit": """
        SELECT p.id, p.title, p.ref AS subtitle, p.status AS severity,
               p.created_at AS ts, NULL::text AS thumbnail_key,
               NULL::text AS camera_external_id, NULL::text AS camera_name
        FROM pursuits p WHERE p.id = ANY($1::uuid[])
    """,
    "alert": """
        SELECT a.id, COALESCE(w.value, a.alert_type) AS title, a.alert_type AS subtitle,
               a.status AS severity, a.created_at AS ts, d.thumbnail_key,
               c.external_id AS camera_external_id, c.name AS camera_name
        FROM alerts a
        JOIN detections d ON d.id = a.detection_id
        LEFT JOIN cameras c ON c.id = d.camera_id
        LEFT JOIN watchlist_entries w ON w.id = a.watchlist_entry_id
        WHERE a.id = ANY($1::uuid[])
    """,
}


@router.get("")
async def list_bookmarks(
    target_type: Optional[str] = None,
    mine_only: bool = Query(default=True, description="false shows the whole shift's bookmarks"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    operator: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT * FROM bookmarks
        WHERE ($1::text IS NULL OR target_type = $1)
          AND ($2::boolean IS FALSE OR operator = $3)
        ORDER BY created_at DESC LIMIT $4 OFFSET $5
        """,
        target_type, mine_only, operator.subject, limit + 1, offset,
    )
    body = paginate(rows, limit, offset)

    # Resolve the referenced objects in one query per type, not one per bookmark.
    by_type: dict[str, list[str]] = {}
    for row in body["items"]:
        by_type.setdefault(row["target_type"], []).append(str(row["target_id"]))
    resolved: dict[str, dict] = {}
    for kind, ids in by_type.items():
        sql = _RESOLVERS.get(kind)
        if not sql:
            continue
        found = await db.fetch(sql, ids)
        await storage.attach_thumbnails(found)
        for item in found:
            resolved[str(item["id"])] = item
    for row in body["items"]:
        row["target"] = resolved.get(str(row["target_id"]))
    return body


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_bookmark(
    body: BookmarkCreate,
    operator: Operator = Depends(current_operator),
) -> dict:
    tid = as_uuid(body.target_id, "target id")
    row = await db.fetchrow(
        """
        INSERT INTO bookmarks (target_type, target_id, operator, label)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (target_type, target_id, operator) DO UPDATE SET label = EXCLUDED.label
        RETURNING *
        """,
        body.target_type, tid, operator.subject, body.label,
    )
    await audit(operator, "bookmark.create", target=tid, target_type=body.target_type)
    return row


@router.delete("/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bookmark(
    bookmark_id: str,
    operator: Operator = Depends(current_operator),
) -> None:
    bid = as_uuid(bookmark_id, "bookmark id")
    # Scoped to the caller: an operator cannot clear a colleague's shortlist.
    deleted = await db.fetch(
        "DELETE FROM bookmarks WHERE id = $1 AND operator = $2 RETURNING id", bid, operator.subject
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such bookmark of yours")
    await audit(operator, "bookmark.delete", target=bid)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_by_target(
    target_type: str,
    target_id: str,
    operator: Operator = Depends(current_operator),
) -> None:
    """Un-bookmark by what it points at — what a toggle button in the UI has to hand."""
    tid = as_uuid(target_id, "target id")
    await db.execute(
        "DELETE FROM bookmarks WHERE target_type = $1 AND target_id = $2 AND operator = $3",
        target_type, tid, operator.subject,
    )
    await audit(operator, "bookmark.delete", target=tid, target_type=target_type)
