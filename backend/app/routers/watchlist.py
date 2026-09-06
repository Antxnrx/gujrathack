"""The watchlist that ``ml/worker.py`` matches every plate read against.

Plates are stored normalised (upper-case alphanumerics only) because that is what
``ml/plate_validator.py`` produces. An entry typed as "GJ 01 AB 1234" would never
match a read of "GJ01AB1234", and the failure would be silent — a wanted vehicle
passing a camera and no alert.

Adding an entry is checked against history immediately. A watchlist that only
matches *future* sightings is missing the obvious question an operator has when
they add a plate: has this vehicle already been through? So the response includes
the retrospective hits, and the endpoint offers to raise alerts for them.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, audit
from backend.app.schemas import WatchlistCreate
from backend.app.security import Operator, current_operator, require_role

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("")
async def list_entries(
    q: Optional[str] = None,
    type_filter: Optional[str] = Query(default=None, alias="type"),
    _: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT w.id, w.type, w.value, w.reason, w.created_at,
               w.reference_embedding IS NOT NULL AS has_embedding,
               COUNT(a.id)  AS alert_count,
               MAX(a.created_at) AS last_alert_at,
               COUNT(a.id) FILTER (WHERE a.status = 'pending') AS pending_alerts
        FROM watchlist_entries w
        LEFT JOIN alerts a ON a.watchlist_entry_id = w.id
        WHERE ($1::text IS NULL OR w.value ILIKE '%' || $1 || '%' OR w.reason ILIKE '%' || $1 || '%')
          AND ($2::text IS NULL OR w.type = $2)
        GROUP BY w.id ORDER BY w.created_at DESC
        """,
        q, type_filter,
    )
    return {"entries": rows, "count": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_role("operator"))])
async def create_entry(
    body: WatchlistCreate,
    backfill: bool = Query(default=False, description="raise alerts for matches already in history"),
    operator: Operator = Depends(current_operator),
) -> dict:
    if body.type == "plate" and not body.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A plate entry needs a value")

    row = await db.fetchrow(
        "INSERT INTO watchlist_entries (type, value, reason) VALUES ($1, $2, $3) RETURNING *",
        body.type, body.value, body.reason,
    )

    history = []
    if body.value:
        history = await db.fetch(
            """
            SELECT t.id AS track_id, t.plate_text, t.plate_confidence, t.plate_read_count,
                   t.first_seen, t.last_seen, t.thumbnail_key, t.best_detection_id,
                   c.external_id AS camera_external_id, c.name AS camera_name, c.district
            FROM tracks t JOIN cameras c ON c.id = t.camera_id
            WHERE t.plate_text = $1 ORDER BY t.first_seen DESC LIMIT 100
            """,
            body.value,
        )
        await storage.attach_thumbnails(history)

    raised = 0
    if backfill and history:
        # Insert alerts for the historical sightings. tr_alert_mirror puts each on
        # the timeline, so a plate added at 2am surfaces the 11pm sighting instead
        # of the operator having to search for it separately.
        for hit in history:
            if not hit["best_detection_id"]:
                continue
            await db.execute(
                "INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type) "
                "VALUES ($1, $2, 'watchlist_match')",
                row["id"], hit["best_detection_id"],
            )
            raised += 1

    await audit(operator, "watchlist.create", target=str(row["id"]),
                type=body.type, reason=body.reason,
                historical_matches=len(history), alerts_raised=raised)
    return {"entry": row, "historical_matches": history,
            "historical_count": len(history), "alerts_raised": raised}


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_role("operator"))])
async def delete_entry(entry_id: str, operator: Operator = Depends(current_operator)) -> None:
    eid = as_uuid(entry_id, "entry id")
    existing = await db.fetchrow("SELECT * FROM watchlist_entries WHERE id = $1", eid)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such watchlist entry")

    # alerts.watchlist_entry_id is nullable and has no ON DELETE clause, so the
    # alerts already raised would block the delete. Detaching them keeps the alert
    # history — the sighting happened, and removing the reason it was flagged must
    # not erase the record that it was.
    await db.execute("UPDATE alerts SET watchlist_entry_id = NULL WHERE watchlist_entry_id = $1", eid)
    await db.execute("DELETE FROM watchlist_entries WHERE id = $1", eid)
    await audit(operator, "watchlist.delete", target=eid,
                value=existing["value"], reason=existing["reason"])
