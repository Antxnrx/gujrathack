"""Playbooks — the console arranged by the job, not by the data model.

Why this layer exists
---------------------
Every capability underneath this file already worked. There are endpoints for
tracks, detections, events, alerts, the watchlist, the transition graph,
incidents and evidence, and each is correct on its own. What there was no way to
do was *a piece of police work*, because the console was organised the way the
database is: thirteen surfaces, one per table.

An officer does not arrive wanting to browse detections. They arrive with a job:

    a vehicle was stolen — has it been through?
    a crime happened here at this hour — who was present?
    this is going to court — seal it

Each of those is a fixed sequence over four or five of those endpoints, in an
order that matters, with defaults that matter. Leaving the sequence in the
operator's head means it is performed differently by every officer, on their
worst day as well as their best, and never the same way twice in a case file.
Encoding it here makes the doctrine explicit, auditable, and the same at 3am.

What is deliberately *not* here
-------------------------------
No playbook does anything the underlying endpoints could not. This is
composition and defaults, not a second implementation — a playbook that drifted
from the endpoint it wraps would be worse than no playbook, because the case
file would then describe work that did not happen.

Every response carries ``caveats``
----------------------------------
This is the part that matters most and is the easiest to leave out. A playbook
returns what it found *and what could be wrong with it*, drawn from this
system's own measurements rather than from a disclaimer: that a plate hit is a
one-character inference rather than an exact read; that only 7 of 30 cameras on
this estate can read a plate at all, so silence is not absence; that the archive
covers twelve hours and a canvass outside them will return nothing for reasons
that have nothing to do with the vehicle.

An investigator who is told "no sightings" will act on it. Whether that means
"the vehicle was not here" or "we cannot see plates at these cameras" is the
whole difference, and only this layer is in a position to say which.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app import db, storage
from backend.app.common import as_uuid, audit, utc
from backend.app.routers import evidence as evidence_router
from backend.app.routers import incidents as incidents_router
from backend.app.routers import watchlist as watchlist_router
from backend.app.schemas import EvidenceCreate, IncidentCreate
from backend.app.security import Operator, current_operator, require_role

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


# ─────────────────────────────────────────────────────────────────────────────
# The catalogue
# ─────────────────────────────────────────────────────────────────────────────
# Served rather than hard-coded in the client so that the console, the API docs
# and anyone reading the audit log see the same list. `produces` names what ends
# up in the case file, which is the only reason an officer runs any of these.
CATALOGUE: list[dict[str, Any]] = [
    {
        "id": "wanted-vehicle",
        "name": "Wanted vehicle",
        "question": "This vehicle is wanted — has it been through, and tell me if it returns.",
        "when": "A stolen-vehicle report, a suspect vehicle named in an FIR, a "
                "registration from a complainant.",
        "needs": ["plate", "reason"],
        "produces": ["watchlist entry", "alerts for past sightings", "an open incident"],
        "endpoint": "POST /api/playbooks/wanted-vehicle",
        "requires": "operator",
        "note": "Searches history as well as arming the future. The index keeps "
                "growing, so re-run the scan later from the watchlist entry.",
    },
    {
        "id": "scene-canvass",
        "name": "Scene canvass",
        "question": "Something happened here, in this window — what was present?",
        "when": "A reported offence with a location and a time but no suspect "
                "vehicle. The digital equivalent of knocking on doors.",
        "needs": ["cameras or district", "from", "to"],
        "produces": ["every vehicle seen, with its plate and how far to trust it",
                     "a per-camera count", "an open incident"],
        "endpoint": "POST /api/playbooks/scene-canvass",
        "requires": "any signed-in role",
        "note": "Ordered by plate trust, so the vehicles worth acting on are "
                "at the top rather than the ones that happened to pass first.",
    },
    {
        "id": "follow-vehicle",
        "name": "Follow a vehicle",
        "question": "I have this vehicle here — where did it go next?",
        "when": "A sighting exists and the direction of travel is the question.",
        "needs": ["track or plate"],
        "produces": ["ranked onward candidates at neighbouring cameras"],
        "endpoint": "POST /api/pursuits",
        "requires": "any signed-in role",
        "note": "Already an endpoint of its own; listed here so the catalogue "
                "is the whole set of jobs rather than only the ones this file "
                "happens to implement.",
    },
    {
        "id": "court-pack",
        "name": "Seal for court",
        "question": "This is going to a court — freeze what the system says today.",
        "when": "Before a case file leaves the control room.",
        "needs": ["incident"],
        "produces": ["a sealed SHA-256 manifest", "a verification result"],
        "endpoint": "POST /api/playbooks/court-pack",
        "requires": "operator",
        "note": "Seals references and field values, never copies of imagery. "
                "Verification separates a broken seal from ordinary drift.",
    },
]


@router.get("")
async def catalogue(_: Operator = Depends(current_operator)) -> dict:
    """The jobs this system can do, in the words an officer would use."""
    return {"playbooks": CATALOGUE, "count": len(CATALOGUE)}


# ─────────────────────────────────────────────────────────────────────────────
# Shared: what could be wrong with this answer
# ─────────────────────────────────────────────────────────────────────────────
async def _estate_caveats() -> list[str]:
    """Caveats that follow from the estate itself, measured, not asserted.

    Recomputed per call rather than baked in, because the estate changes: more
    cameras get indexed, the archive window moves. A caveat that has gone stale
    is worse than none, since it will be believed.
    """
    # Scalar subqueries rather than a join: the first version joined cameras to
    # tracks on TRUE, which is a cartesian product and made COUNT(*) report
    # sixty thousand cameras.
    row = await db.fetchrow(
        """
        SELECT (SELECT COUNT(*) FROM cameras) AS cameras,
               (SELECT COUNT(*) FROM cameras
                 WHERE metadata->'anpr'->>'tier' = 'anpr_capable') AS anpr,
               (SELECT COUNT(*) FROM cameras
                 WHERE metadata->'anpr'->>'tier' = 'occasional') AS anpr_occasional,
               (SELECT MIN(first_seen) FROM tracks) AS archive_from,
               (SELECT MAX(last_seen)  FROM tracks) AS archive_to
        """
    )
    out: list[str] = []
    if row and row["cameras"]:
        anpr = row["anpr"] or 0
        occasional = row["anpr_occasional"] or 0
        out.append(
            f"{anpr} of {row['cameras']} cameras on this estate read plates reliably and "
            f"{occasional} occasionally; at the remaining "
            f"{row['cameras'] - anpr - occasional}, a vehicle can pass and be seen without "
            f"its registration ever being readable. That is optics, not software — a plate "
            f"needs about 90 px of width and 71% of vehicle crops here are under 120 px "
            f"overall. No plate hit is not the same as no vehicle."
        )
    if row and row["archive_from"] and row["archive_to"]:
        out.append(
            f"The indexed archive runs {row['archive_from']:%d %b %H:%M} to "
            f"{row['archive_to']:%d %b %H:%M}. A window outside it returns nothing for "
            f"reasons that have nothing to do with the vehicle."
        )
    return out


def _plate_caveats(hits: list[dict]) -> list[str]:
    inferred = [h for h in hits if h.get("match") == "confusion"]
    out: list[str] = []
    if inferred:
        worst = min(h["match_score"] for h in inferred)
        out.append(
            f"{len(inferred)} of {len(hits)} sightings matched on confusion distance, not "
            f"an exact read — lowest score {worst:.3f}. The reader is 67% exactly right and "
            f"its errors are almost all one glyph (O against Q, 6 against 4), so this is how "
            f"a real vehicle is found despite a misread. Confirm against the crop before "
            f"acting."
        )
    weak = [h for h in hits if (h.get("plate_trust") or {}).get("tier") == "lead"]
    if weak:
        out.append(
            f"{len(weak)} sighting(s) rest on a plate in the weakest confidence band, which "
            f"was exactly right 0 times out of 1 in measurement. Treat as a lead only."
        )
    return out


def _contradiction_caveats(tracks: list[dict]) -> list[str]:
    """Where the canvass contradicts itself, say so before the officer finds it.

    A registration is unique to a vehicle, so the same plate read on two
    different vehicle classes — or two different colours — inside one short
    window is proof that at least one of the two reads is wrong. This estate
    does it: ``GJ11CQ5760`` appears on a white car at 00:13:20 and a red truck
    at 00:14:45.

    Nothing else in the system is placed to notice. Each read is individually
    plausible, each passes the grammar, each carries a reasonable confidence;
    it is only *together*, in one list, that they are impossible. An officer
    who spots it loses trust in the whole canvass; an officer who does not may
    dispatch on the wrong vehicle. Either way it is better said out loud, and
    it costs one pass over a list already in memory.
    """
    seen: dict[str, set[tuple]] = {}
    for t in tracks:
        if not t.get("plate_text"):
            continue
        seen.setdefault(t["plate_text"], set()).add(
            (t.get("class"), t.get("dominant_color")))
    clashes = {p: v for p, v in seen.items() if len(v) > 1}
    if not clashes:
        return []
    shown = "; ".join(
        f"{p} reads as " + " and ".join(
            f"{c or 'unknown'} {k or 'vehicle'}" for k, c in sorted(v, key=str))
        for p, v in list(clashes.items())[:3])
    return [
        f"{len(clashes)} registration(s) appear on more than one kind of vehicle in this "
        f"window, which cannot be true of a real plate — {shown}. At least one read in "
        f"each pair is wrong; check the crops before treating either as an identification."
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Playbook: wanted vehicle
# ─────────────────────────────────────────────────────────────────────────────
class WantedVehicle(BaseModel):
    plate: Annotated[str, Field(min_length=4, max_length=32)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    priority: Literal["low", "medium", "high", "critical"] = "high"
    threshold: Annotated[float, Field(ge=0.5, le=1.0)] = \
        watchlist_router.DEFAULT_MATCH_THRESHOLD
    open_incident: bool = True


@router.post("/wanted-vehicle", dependencies=[Depends(require_role("operator"))])
async def wanted_vehicle(
    body: WantedVehicle,
    operator: Operator = Depends(current_operator),
) -> dict:
    """Arm the future and search the past, in one action.

    These are two halves of one question and were two separate operations: the
    watchlist told the worker to alert on future reads, and finding out whether
    the vehicle had *already* been through meant knowing to go and search for it.
    For a stolen vehicle reported hours after the fact, the past half is usually
    the half that matters.
    """
    plate = "".join(ch for ch in body.plate.upper() if ch.isalnum())

    entry = await db.fetchrow(
        "SELECT * FROM watchlist_entries WHERE type='plate' AND upper(value)=$1", plate)
    if entry is None:
        entry = await db.fetchrow(
            "INSERT INTO watchlist_entries (type, value, reason) "
            "VALUES ('plate', $1, $2) RETURNING *", plate, body.reason)
        entry_is_new = True
    else:
        entry_is_new = False

    hits = await watchlist_router._history_for(plate, body.threshold)

    raised = already = 0
    alert_ids: list[str] = []
    for hit in hits:
        if not hit["best_detection_id"]:
            continue
        seen = await db.fetchrow(
            "SELECT id FROM alerts WHERE watchlist_entry_id = $1 AND detection_id = $2",
            entry["id"], hit["best_detection_id"])
        if seen:
            already += 1
            alert_ids.append(str(seen["id"]))
            continue
        new = await db.fetchrow(
            "INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type) "
            "VALUES ($1, $2, 'watchlist_match') RETURNING id",
            entry["id"], hit["best_detection_id"])
        alert_ids.append(str(new["id"]))
        raised += 1

    # tr_alert_mirror puts every alert on the timeline as an event, so these are
    # the rows the incident should carry. Opening an incident and leaving it
    # empty is worse than not opening one: it looks like work that was done.
    event_ids: list[str] = []
    if alert_ids:
        rows = await db.fetch(
            "SELECT id FROM events WHERE alert_id = ANY($1::uuid[]) ORDER BY started_at",
            [as_uuid(a, "alert id") for a in alert_ids])
        event_ids = [str(r["id"]) for r in rows]

    incident = None
    if body.open_incident and hits:
        by_time = sorted(hits, key=lambda h: h["first_seen"])
        first, last = by_time[0], by_time[-1]
        incident = await incidents_router.create_incident(
            IncidentCreate(
                title=f"Wanted vehicle {plate}",
                description=(
                    f"{body.reason}\n\n"
                    f"{len(hits)} past sighting(s) found by retrospective scan at threshold "
                    f"{body.threshold:.2f}. First {first['first_seen']:%d %b %H:%M} at "
                    f"{first['camera_external_id']}, most recent "
                    f"{last['first_seen']:%d %b %H:%M} at {last['camera_external_id']}. "
                    f"The watchlist entry remains armed for future sightings."),
                priority=body.priority,
                event_ids=event_ids,
            ),
            operator=operator,
        )

    caveats = _plate_caveats(hits) + await _estate_caveats()
    if not hits:
        caveats.insert(0, "No past sighting found. The entry is armed for future reads, "
                          "and re-running the scan later will cover footage indexed since.")

    await audit(operator, "playbook.wanted_vehicle", target=str(entry["id"]),
                plate=plate, matches=len(hits), alerts_raised=raised)

    return {
        "playbook": "wanted-vehicle",
        "asked": {"plate": plate, "reason": body.reason, "threshold": body.threshold},
        "found": {
            "sightings": hits,
            "sighting_count": len(hits),
            "exact": sum(1 for h in hits if h["match"] == "exact"),
            "inferred": sum(1 for h in hits if h["match"] == "confusion"),
            "cameras": sorted({h["camera_external_id"] for h in hits}),
        },
        "created": {
            "watchlist_entry": entry, "watchlist_entry_is_new": entry_is_new,
            "alerts_raised": raised, "alerts_already_known": already,
            "incident": incident, "events_attached": len(event_ids),
        },
        "next_steps": [
            "Open each sighting's crop and confirm the registration by eye before "
            "dispatching — an inferred match is one character of evidence short.",
            "Run Follow a vehicle from the most recent sighting to establish "
            "direction of travel.",
            "Re-run this scan after the next indexing pass.",
        ],
        "caveats": caveats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Playbook: scene canvass
# ─────────────────────────────────────────────────────────────────────────────
class SceneCanvass(BaseModel):
    cameras: list[str] = Field(default_factory=list, max_length=40,
                               description="external ids or uuids; empty means the district")
    district: str | None = None
    from_ts: datetime
    to_ts: datetime
    limit: Annotated[int, Field(ge=1, le=500)] = 200
    open_incident: bool = False
    title: Annotated[str, Field(max_length=300)] | None = None


@router.post("/scene-canvass")
async def scene_canvass(
    body: SceneCanvass,
    operator: Operator = Depends(current_operator),
) -> dict:
    """Everything that passed a place in a window, ordered by what is actionable.

    The ordering is the whole point and is not chronological. A canvass returning
    two hundred vehicles in time order asks the officer to read all two hundred;
    the same list with the confidently-read plates first is a list they can stop
    reading. Vehicles with no readable plate are kept, last — they are still a
    make, a colour and a time, which is what a witness statement is made of.
    """
    if body.to_ts <= body.from_ts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "'to' must be after 'from'")
    if not body.cameras and not body.district:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "A canvass needs somewhere to canvass — cameras or a district")
    # Reading who was present is a read, and an analyst is entitled to it. What
    # needs the write role is opening a case file, so the check sits on that and
    # not on the whole playbook — otherwise the read-only role, which is the one
    # most likely to be doing background research, cannot do the one job it is
    # for.
    if body.open_incident and not operator.can("operator"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your role can run a canvass but not open a case file from it. "
            "Re-run with open_incident off, or ask an operator to open the incident.")

    cam_ids: list[str] = []
    if body.cameras:
        rows = await db.fetch(
            "SELECT id, external_id FROM cameras "
            "WHERE external_id = ANY($1::text[]) OR camera_uid = ANY($1::text[])",
            body.cameras)
        cam_ids = [str(r["id"]) for r in rows]
        missing = set(body.cameras) - {r["external_id"] for r in rows}
        if missing and not cam_ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"No such camera: {', '.join(sorted(missing))}")

    tracks = await db.fetch(
        """
        SELECT t.id, t.plate_text, t.plate_confidence, t.plate_read_count,
               t.class, t.dominant_color, t.class_votes, t.color_votes,
               t.first_seen, t.last_seen, t.duration_s,
               t.thumbnail_key, t.best_detection_id,
               c.external_id AS camera_external_id, c.name AS camera_name, c.district
        FROM tracks t JOIN cameras c ON c.id = t.camera_id
        WHERE t.first_seen < $2 AND t.last_seen >= $1
          AND ($3::uuid[] IS NULL OR t.camera_id = ANY($3::uuid[]))
          AND ($4::text  IS NULL OR c.district = $4)
        ORDER BY t.first_seen
        LIMIT $5
        """,
        utc(body.from_ts), utc(body.to_ts),
        [as_uuid(c, "camera id") for c in cam_ids] if cam_ids else None,
        body.district, body.limit,
    )
    await storage.attach_thumbnails(tracks)
    storage.attach_plate_trust(tracks)
    storage.attach_appearance(tracks)

    # Actionable first, then verify, then leads, then no plate at all. Within a
    # band, earliest first — inside one band the time is the useful order.
    rank = {"actionable": 0, "verify": 1, "lead": 2}
    tracks.sort(key=lambda t: (
        rank.get((t.get("plate_trust") or {}).get("tier"), 3), t["first_seen"]))

    by_camera: dict[str, int] = {}
    for t in tracks:
        by_camera[t["camera_external_id"]] = by_camera.get(t["camera_external_id"], 0) + 1
    # People are tracked here too, and a canvass should return them — someone on
    # foot at the scene is exactly what this job is asking about. They are
    # counted apart from vehicles because calling five pedestrians "vehicles
    # present" is the kind of small untruth that makes an officer stop believing
    # the rest of the number.
    people = [t for t in tracks if t["class"] == "person"]
    vehicles = [t for t in tracks if t["class"] != "person"]
    with_plate = [t for t in tracks if t["plate_text"]]
    actionable = [t for t in with_plate
                  if (t.get("plate_trust") or {}).get("tier") == "actionable"]

    incident = None
    if body.open_incident:
        where = ", ".join(sorted(by_camera)) or body.district or "the selected area"
        incident = await incidents_router.create_incident(
            IncidentCreate(
                title=body.title or f"Canvass — {where}, {body.from_ts:%d %b %H:%M}",
                description=(
                    f"Canvass of {where} between {body.from_ts:%d %b %H:%M} and "
                    f"{body.to_ts:%d %b %H:%M}. {len(vehicles)} vehicle(s) and "
                    f"{len(people)} person(s) present, "
                    f"{len(with_plate)} with a readable plate, {len(actionable)} of those "
                    f"in the band that can be acted on directly."),
                priority="medium",
            ),
            operator=operator,
        )

    caveats = _contradiction_caveats(tracks) + await _estate_caveats()
    if len(tracks) >= body.limit:
        caveats.insert(0, f"Truncated at {body.limit} vehicles — the window holds more. "
                          f"Narrow the time or the cameras rather than reading a "
                          f"partial list as the whole scene.")
    if with_plate and not actionable:
        caveats.append("No vehicle in this window has a plate in the actionable band. "
                       "The registrations shown are leads for confirmation against the "
                       "crop, not identifications.")

    await audit(operator, "playbook.scene_canvass",
                cameras=sorted(by_camera), district=body.district,
                window=[body.from_ts.isoformat(), body.to_ts.isoformat()],
                vehicles=len(tracks))

    return {
        "playbook": "scene-canvass",
        "asked": {"cameras": body.cameras, "district": body.district,
                  "from": body.from_ts.isoformat(), "to": body.to_ts.isoformat()},
        "found": {
            "vehicles": tracks,
            "vehicle_count": len(vehicles),
            "people_count": len(people),
            "present_count": len(tracks),
            "with_readable_plate": len(with_plate),
            "actionable_plates": len(actionable),
            "by_camera": by_camera,
        },
        "created": {"incident": incident},
        "next_steps": [
            "Work down from the top — the list is ordered by how far each plate "
            "can be trusted, not by time.",
            "Put any vehicle of interest on the watchlist with Wanted vehicle; "
            "that also searches the rest of the archive for it.",
            "Seal the incident with Seal for court once the shortlist is settled.",
        ],
        "caveats": caveats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Playbook: seal for court
# ─────────────────────────────────────────────────────────────────────────────
class CourtPack(BaseModel):
    incident_id: str
    note: Annotated[str, Field(max_length=4000)] = ""


@router.post("/court-pack", dependencies=[Depends(require_role("operator"))])
async def court_pack(
    body: CourtPack,
    operator: Operator = Depends(current_operator),
) -> dict:
    """Seal everything attached to an incident, then immediately verify the seal.

    Verifying straight after sealing looks redundant and is not. It proves the
    manifest that was stored is the manifest that was hashed, at the moment of
    sealing, when a mismatch is still a bug someone can fix — rather than six
    months later in a courtroom, where the same mismatch is indistinguishable
    from tampering.
    """
    iid = as_uuid(body.incident_id, "incident id")
    incident = await db.fetchrow("SELECT * FROM incidents WHERE id = $1", iid)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such incident")

    events = await db.fetch(
        "SELECT event_id FROM incident_events WHERE incident_id = $1 ORDER BY sequence_no", iid)
    event_ids = [str(e["event_id"]) for e in events]
    if not event_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This incident has nothing attached — a snapshot over no items would "
            "hash to a claim about nothing")

    # The tracks behind those events, so the pack carries the vehicle rows an
    # officer will be asked about and not only the alerts that flagged them.
    tracks = await db.fetch(
        "SELECT DISTINCT track_uuid FROM events "
        "WHERE id = ANY($1::uuid[]) AND track_uuid IS NOT NULL",
        [as_uuid(e, "event id") for e in event_ids])
    track_ids = [str(t["track_uuid"]) for t in tracks]

    snapshot = await evidence_router.create_snapshot(
        EvidenceCreate(
            title=f"{incident['title']} — sealed record",
            incident_id=str(iid),
            event_ids=event_ids,
            track_ids=track_ids,
            note=body.note or (
                "Sealed from the incident as it stands. Records the rows as they read "
                "now — the plate, its confidence, the reads that voted on it, the camera "
                "and the time. No imagery is copied; the snapshot references the crops "
                "where they already live."),
        ),
        operator=operator,
    )
    verification = await evidence_router.verify(str(snapshot["id"]), operator=operator)

    intact = verification.get("manifest_intact")
    drift = verification.get("drift_count", 0)
    caveats: list[str] = []
    if not intact:
        caveats.append("The manifest hash does not re-derive. Do not rely on this pack — "
                       "this is a fault to raise, not a finding to explain.")
    if drift:
        caveats.append(
            f"{drift} referenced row(s) have changed since sealing. That is the ordinary "
            f"case for an older snapshot and is reported separately from the seal: the "
            f"seal is about the snapshot, drift is about the world moving on.")
    caveats.append("The pack seals what the system believed at this moment. Where a plate "
                   "is marked unverified or low confidence, that qualification is part of "
                   "the record and travels with it.")

    await audit(operator, "playbook.court_pack", target=str(snapshot["id"]),
                incident=str(iid), sha256=snapshot.get("sha256"), intact=intact)

    return {
        "playbook": "court-pack",
        "asked": {"incident_id": str(iid), "incident_title": incident["title"]},
        "found": {"events_sealed": len(event_ids), "tracks_sealed": len(track_ids)},
        "created": {"snapshot": snapshot, "verification": verification},
        "next_steps": [
            "Record the SHA-256 in the case file; it is what a later verification "
            "is checked against.",
            "Re-verify before the pack is relied on — /api/evidence/{id}/verify "
            "needs no special access and separates a broken seal from drift.",
        ],
        "caveats": caveats,
    }
