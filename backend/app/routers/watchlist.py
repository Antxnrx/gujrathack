"""The watchlist that ``ml/worker.py`` matches every plate read against.

Plates are stored normalised (upper-case alphanumerics only) because that is what
``ml/plate_validator.py`` produces. An entry typed as "GJ 01 AB 1234" would never
match a read of "GJ01AB1234", and the failure would be silent — a wanted vehicle
passing a camera and no alert.

Adding an entry is checked against history immediately. A watchlist that only
matches *future* sightings is missing the obvious question an operator has when
they add a plate: has this vehicle already been through? So the response includes
the retrospective hits, and the endpoint offers to raise alerts for them.

That history is matched on confusion distance, not string equality. The reader is
67% exact, and its errors are overwhelmingly one glyph — so a vehicle wanted as
``GJ11CO5760`` sits in this index as ``GJ11CQ5760``. An exact match answers "never
seen", which is the one wrong answer indistinguishable from the truth. See
``_history_for``.

``POST /watchlist/{id}/scan`` re-asks the same question later, because the index
keeps growing after an entry is added and footage indexed this afternoon has
never been compared against a plate added this morning.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, audit
from backend.app.schemas import WatchlistCreate
from backend.app.security import Operator, current_operator, require_role
from ml.analytics.anpr import plate_similarity

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

# Below this two plates are different vehicles. 0.90 admits one confusion-class
# substitution in a ten-character plate and rejects two unrelated registrations,
# which is the line that matters: a false hit sends a unit to the wrong vehicle.
DEFAULT_MATCH_THRESHOLD = 0.90

# Trigram floor for the SQL prefilter. Deliberately loose — it exists so the
# confusion-aware scorer runs over tens of rows instead of the whole index, and
# anything it lets through is rejected properly a moment later. Too tight here
# and a real match is lost before the scorer ever sees it.
_TRIGRAM_PREFILTER = 0.35


async def _history_for(value: str, threshold: float) -> list[dict]:
    """Every past sighting of this plate, matched the way the reader reads.

    Exact string equality is the wrong test when ANPR is 67% accurate. A vehicle
    recorded as ``GJ11CO5760`` sits in this index as ``GJ11CQ5760`` — one
    confusion-class substitution, a Q whose tail is about one pixel at these
    resolutions. Asked for the correct plate, an exact match returns nothing,
    and the officer is told a wanted vehicle has never been seen. That is the
    worst answer this endpoint can give, because it is indistinguishable from
    the true negative.

    ``plate_similarity`` costs a confusion-class substitution a quarter of a
    normal one, so it scores that pair at 0.975 and two unrelated registrations
    below 0.2. The trigram index does the coarse pass in SQL; the scorer decides.

    Each row records *how* it matched, so an operator can tell an exact hit from
    a one-character inference before acting on it.
    """
    candidates = await db.fetch(
        """
        SELECT t.id AS track_id, t.plate_text, t.plate_confidence, t.plate_read_count,
               t.first_seen, t.last_seen, t.thumbnail_key, t.best_detection_id,
               c.external_id AS camera_external_id, c.name AS camera_name, c.district
        FROM tracks t JOIN cameras c ON c.id = t.camera_id
        WHERE t.plate_text IS NOT NULL
          AND (t.plate_text = $1 OR similarity(t.plate_text, $1) >= $2)
        ORDER BY t.first_seen DESC LIMIT 500
        """,
        value, _TRIGRAM_PREFILTER,
    )
    hits: list[dict] = []
    for r in candidates:
        sim = plate_similarity(value, r["plate_text"])
        if sim < threshold:
            continue
        r["match_score"] = round(sim, 4)
        r["match"] = "exact" if r["plate_text"] == value else "confusion"
        hits.append(r)
    hits.sort(key=lambda r: (-r["match_score"], r["first_seen"]))
    await storage.attach_thumbnails(hits)
    storage.attach_plate_trust(hits)
    storage.attach_appearance(hits)
    return hits


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
    threshold: float = Query(default=DEFAULT_MATCH_THRESHOLD, ge=0.5, le=1.0,
                             description="confusion-aware match floor; 1.0 is exact-only"),
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
        history = await _history_for(body.value, threshold)

    raised = 0
    if backfill and history:
        # Insert alerts for the historical sightings. tr_alert_mirror puts each on
        # the timeline, so a plate added at 2am surfaces the 11pm sighting instead
        # of the operator having to search for it separately.
        #
        # One alert per *track*, which is one per vehicle pass. _history_for has
        # already collapsed the frames: a single car crossing cam06 was read on
        # ten consecutive frames and produced ten identical alerts, which is not
        # ten pieces of information — it is one, repeated until the operator
        # stops reading the queue.
        for hit in history:
            if not hit["best_detection_id"]:
                continue
            await db.execute(
                "INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type) "
                "VALUES ($1, $2, 'watchlist_match')",
                row["id"], hit["best_detection_id"],
            )
            raised += 1

    exact = sum(1 for h in history if h["match"] == "exact")
    await audit(operator, "watchlist.create", target=str(row["id"]),
                type=body.type, reason=body.reason,
                historical_matches=len(history), alerts_raised=raised,
                exact_matches=exact, threshold=threshold)
    return {"entry": row, "historical_matches": history,
            "historical_count": len(history), "exact_count": exact,
            "confusion_count": len(history) - exact,
            "match_threshold": threshold, "alerts_raised": raised}


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
    await db.execute(
        "UPDATE alerts SET watchlist_entry_id = NULL WHERE watchlist_entry_id = $1", eid)
    await db.execute("DELETE FROM watchlist_entries WHERE id = $1", eid)
    await audit(operator, "watchlist.delete", target=eid,
                value=existing["value"], reason=existing["reason"])


@router.post("/{entry_id}/scan", dependencies=[Depends(require_role("operator"))])
async def scan_history(
    entry_id: str,
    raise_alerts: bool = Query(default=True),
    threshold: float = Query(default=DEFAULT_MATCH_THRESHOLD, ge=0.5, le=1.0),
    operator: Operator = Depends(current_operator),
) -> dict:
    """Re-ask "has this vehicle been seen?" for an entry already on the list.

    ``ml/worker.py`` checks each detection as it is produced, which answers
    "tell me when this plate appears next". That is half the question an
    investigator has. The other half — *has it appeared already* — needs the
    index scanned, and the index keeps growing after an entry is added: footage
    indexed this afternoon has never been compared against a plate added this
    morning. This is the endpoint that closes that gap, and it is idempotent, so
    running it on a schedule is safe.
    """
    eid = as_uuid(entry_id, "entry id")
    entry = await db.fetchrow("SELECT * FROM watchlist_entries WHERE id = $1", eid)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such watchlist entry")
    if not entry["value"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This entry has no plate to scan for")

    history = await _history_for(entry["value"], threshold)

    raised = already = 0
    if raise_alerts:
        for hit in history:
            if not hit["best_detection_id"]:
                continue
            # Re-running must not produce a second copy of a hit an operator
            # has already triaged.
            seen = await db.fetchval(
                "SELECT 1 FROM alerts WHERE watchlist_entry_id = $1 AND detection_id = $2",
                eid, hit["best_detection_id"],
            )
            if seen:
                already += 1
                continue
            await db.execute(
                "INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type) "
                "VALUES ($1, $2, 'watchlist_match')",
                eid, hit["best_detection_id"],
            )
            raised += 1

    exact = sum(1 for h in history if h["match"] == "exact")
    await audit(operator, "watchlist.scan", target=eid, value=entry["value"],
                matches=len(history), alerts_raised=raised, threshold=threshold)
    return {"entry": entry, "matches": history, "match_count": len(history),
            "exact_count": exact, "confusion_count": len(history) - exact,
            "alerts_raised": raised, "already_known": already,
            "match_threshold": threshold}
