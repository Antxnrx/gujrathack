"""Cross-camera reconstruction, as an API.

A *pursuit* is a saved investigation: one subject, one seed sighting, and the ranked
candidates the engine found for it across the estate — each with the evidence that
produced its score, and each awaiting a human verdict.

The endpoints deliberately separate the three things that get conflated elsewhere:

* ``POST /pursuits`` — seed and run. Writes candidates, never confirmations.
* ``PATCH /pursuits/{id}/sightings/{sighting_id}`` — the operator's verdict. This is
  the only thing that turns a candidate into a fact, and the only thing that teaches
  the camera graph.
* ``POST /pursuits/{id}/rerun`` — re-score after the graph has learned something, or
  after a batch index has added cameras the first run could not see.

The scoring lives in ``services/dragnet.py``; read its module docstring for why every
number is a likelihood ratio rather than a confidence.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import bus, db, storage
from backend.app.common import as_uuid, audit, paginate
from backend.app.config import settings
from backend.app.schemas import DragnetStart, SightingReview
from backend.app.security import Operator, current_operator
from backend.app.services import dragnet

log = logging.getLogger("setu.api.pursuits")
router = APIRouter(prefix="/pursuits", tags=["pursuits"])

_REF = "'PUR-' || to_char(NOW(), 'YYYY') || '-' || lpad(nextval('pursuit_ref_seq')::text, 4, '0')"


_OBJECT_CLASSES = {"backpack", "suitcase", "handbag", "bag"}


def _subject_kind(vclass: Optional[str]) -> str:
    """Map a detector class onto the pursuits CHECK constraint (vehicle|person|object)."""
    if vclass == "person":
        return "person"
    if vclass in _OBJECT_CLASSES:
        return "object"
    return "vehicle"


async def _seed_from_plate(plate: str) -> str | None:
    """Best track for a plate: most reads first, then most confident.

    Read count before confidence deliberately. Accuracy by number of reads is measured
    at 13.6% for one read and 97.4% for seven; the reported confidence is calibrated
    but coarser, so the track with more frames behind it is the better anchor even when
    another track claims a higher number.
    """
    row = await db.fetchrow(
        """
        SELECT id FROM tracks
        WHERE plate_text = $1
        ORDER BY plate_read_count DESC, plate_confidence DESC NULLS LAST, first_seen
        LIMIT 1
        """,
        plate,
    )
    return str(row["id"]) if row else None


@router.post("", status_code=status.HTTP_201_CREATED)
async def start(body: DragnetStart, operator: Operator = Depends(current_operator)) -> dict:
    seed_track = as_uuid(body.track_uuid, "track id") if body.track_uuid else None
    seed_detection = as_uuid(body.detection_id, "detection id") if body.detection_id else None
    if not seed_track and not seed_detection and body.plate_text:
        seed_track = await _seed_from_plate(body.plate_text)
        if seed_track is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No track carries the consensus plate {body.plate_text}",
            )
    if not seed_track and not seed_detection:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Give a track_uuid, a detection_id, or a plate_text")

    anchor = await dragnet.load_anchor(track_uuid=seed_track, detection_id=seed_detection)
    if anchor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That seed does not exist")

    result = await dragnet.run(
        anchor,
        horizon_minutes=body.horizon_minutes,
        max_hops=body.max_hops,
        min_evidence_ratio=body.min_evidence_ratio,
        min_score=body.min_score,
    )

    title = body.title or (
        f"{anchor.color + ' ' if anchor.color else ''}{anchor.vclass}"
        f"{' ' + anchor.plate if anchor.plate else ''} from {anchor.camera_name or 'camera'}"
    )
    pursuit = await db.fetchrow(
        f"""
        INSERT INTO pursuits (ref, title, subject_kind, subject_class, subject_color,
                              plate_text, seed_detection_id, seed_track_uuid, seed_camera_id,
                              seed_ts, embedding, status, incident_id, params, created_by)
        VALUES ({_REF}, $1, $2, $3, $4, $5, $6, $7, $8, $9,
                CASE WHEN $10::text IS NULL THEN NULL ELSE $10::vector END,
                'active', $11, $12::jsonb, $13)
        RETURNING id, ref, title, status, created_at
        """,
        title,
        _subject_kind(anchor.vclass),
        anchor.vclass, anchor.color, anchor.plate, anchor.detection_id, anchor.track_uuid,
        anchor.camera_id, anchor.ts,
        db.to_vector_literal(anchor.embedding) if anchor.has_embedding else None,
        as_uuid(body.incident_id, "incident id") if body.incident_id else None,
        {
            "horizon_minutes": body.horizon_minutes, "max_hops": body.max_hops,
            "min_evidence_ratio": body.min_evidence_ratio, "min_score": body.min_score,
            "thresholds": result.get("thresholds"),
        },
        operator.label,
    )

    written = {"sightings_written": 0, "predictions_written": 0}
    if body.persist:
        written = await dragnet.persist(str(pursuit["id"]), anchor, result)

    await audit(operator, "pursuit.start", target=str(pursuit["id"]),
                ref=pursuit["ref"], candidates=len(result["candidates"]),
                cells=result.get("cells_scored"))
    # bus.PURSUITS, not a topic of our own: Subscription.wants() drops any topic
    # outside bus.CHANNELS, and the SSE event name is the topic. `kind` discriminates
    # API-originated messages from the tr_pursuit_notify trigger's payload, which
    # carries no kind.
    bus.broker.publish(bus.PURSUITS, {
        "kind": "pursuit_started",
        "pursuit_id": str(pursuit["id"]), "ref": pursuit["ref"], "title": title,
        "candidates": len(result["candidates"]),
    })

    for cand in result["candidates"][:60]:
        await storage.attach_thumbnails([cand])
    return {
        "pursuit": pursuit,
        "anchor": _anchor_public(anchor),
        "candidates": result["candidates"][:60],
        "predictions": result["predictions"],
        "diagnostics": {k: v for k, v in result.items()
                        if k not in ("candidates", "predictions")},
        "persisted": written,
        "method": _METHOD_NOTE,
    }


_METHOD_NOTE = {
    "framing": "likelihood ratio, not a match score",
    "reads": (
        "score is a posterior probability and folds in the corridor prior and the size "
        "of the candidate pool; evidence_ratio is how many times more consistent the "
        "observation is with the subject than with a random same-class vehicle at that "
        "camera. Rank by score, filter by evidence_ratio."
    ),
    "visual_calibration": (
        "CLIP cosine is compared against a same-class background sampled at that camera, "
        "because 0.76–0.91 is the range between completely different vehicles. The "
        "reported evidence is capped at what the sample size can support."
    ),
    "never_asserted": "confirmed stays NULL until an operator reviews the sighting",
}


def _anchor_public(anchor: dragnet.Anchor) -> dict:
    return {
        "kind": anchor.kind, "track_uuid": anchor.track_uuid,
        "detection_id": anchor.detection_id, "camera_id": anchor.camera_id,
        "camera_name": anchor.camera_name, "ts": anchor.ts, "class": anchor.vclass,
        "color": anchor.color, "plate_text": anchor.plate,
        "plate_confidence": anchor.plate_confidence, "has_embedding": anchor.has_embedding,
        "thumbnail_key": anchor.thumbnail_key,
    }


@router.get("")
async def list_pursuits(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    incident_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT p.id, p.ref, p.title, p.status, p.subject_kind, p.subject_class,
               p.subject_color, p.plate_text, p.seed_ts, p.created_by, p.created_at,
               p.closed_at, p.incident_id,
               c.external_id AS seed_camera, c.name AS seed_camera_name,
               COUNT(s.id) FILTER (WHERE s.match_kind <> 'seed')          AS candidates,
               COUNT(s.id) FILTER (WHERE s.confirmed IS TRUE
                                     AND s.match_kind <> 'seed')          AS confirmed,
               COUNT(s.id) FILTER (WHERE s.confirmed IS NULL)             AS unreviewed,
               MAX(s.ts)   FILTER (WHERE s.confirmed IS TRUE)             AS last_confirmed_ts
        FROM pursuits p
        LEFT JOIN cameras c ON c.id = p.seed_camera_id
        LEFT JOIN pursuit_sightings s ON s.pursuit_id = p.id
        WHERE ($1::text IS NULL OR p.status = $1)
          AND ($2::uuid IS NULL OR p.incident_id = $2::uuid)
        GROUP BY p.id, c.external_id, c.name
        ORDER BY p.created_at DESC
        LIMIT $3 OFFSET $4
        """,
        status_filter,
        as_uuid(incident_id, "incident id") if incident_id else None,
        limit + 1, offset,
    )
    return paginate(rows, limit, offset)


@router.get("/{pursuit_id}")
async def get_pursuit(
    pursuit_id: str,
    include_rejected: bool = Query(default=False),
    _: Operator = Depends(current_operator),
) -> dict:
    pid = as_uuid(pursuit_id, "pursuit id")
    pursuit = await db.fetchrow(
        """
        SELECT p.*, c.external_id AS seed_camera, c.name AS seed_camera_name
        FROM pursuits p LEFT JOIN cameras c ON c.id = p.seed_camera_id
        WHERE p.id = $1::uuid
        """,
        pid,
    )
    if pursuit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pursuit")
    # The seed embedding is 512 floats of no use to a UI and ~6 KB on every read.
    pursuit.pop("embedding", None)

    sightings = await db.fetch(
        """
        SELECT s.id, s.detection_id, s.track_uuid, s.camera_id, s.ts, s.score,
               s.match_kind, s.score_breakdown, s.thumbnail_key, s.confirmed,
               s.reviewed_by, s.sequence_no, s.created_at,
               c.external_id AS camera, c.name AS camera_name, c.district,
               c.lat, c.lng, c.department, c.metadata->>'source_system' AS source_system
        FROM pursuit_sightings s JOIN cameras c ON c.id = s.camera_id
        WHERE s.pursuit_id = $1::uuid
          AND ($2::boolean OR s.confirmed IS DISTINCT FROM FALSE)
        ORDER BY s.ts
        """,
        pid, include_rejected,
    )
    await storage.attach_thumbnails(sightings)

    predictions = await db.fetch(
        """
        SELECT p.id, p.camera_id, p.from_camera_id, p.eta_from, p.eta_to, p.probability,
               p.hops, p.via, p.resolved_by, p.expired,
               c.external_id AS camera, c.name AS camera_name, c.lat, c.lng, c.district,
               f.external_id AS from_camera
        FROM pursuit_predictions p
        JOIN cameras c ON c.id = p.camera_id
        LEFT JOIN cameras f ON f.id = p.from_camera_id
        WHERE p.pursuit_id = $1::uuid
        ORDER BY p.expired, p.probability DESC
        """,
        pid,
    )

    # The confirmed subset is the reconstructed route: the only sequence the system
    # will assert, in the only order that means anything (time).
    route = [
        {"camera": s["camera"], "camera_name": s["camera_name"], "ts": s["ts"],
         "lat": s["lat"], "lng": s["lng"], "district": s["district"],
         "match_kind": s["match_kind"], "score": s["score"],
         "sighting_id": str(s["id"])}
        for s in sightings
        if s["confirmed"] is True
    ]
    return {
        "pursuit": pursuit,
        "sightings": sightings,
        "predictions": predictions,
        "route": route,
        "counts": {
            "candidates": sum(1 for s in sightings if s["match_kind"] != "seed"),
            "confirmed": len(route) - (1 if any(s["match_kind"] == "seed" for s in sightings) else 0),
            "unreviewed": sum(1 for s in sightings if s["confirmed"] is None),
            "predictions_open": sum(
                1 for p in predictions if not p["expired"] and p["resolved_by"] is None
            ),
        },
        "method": _METHOD_NOTE,
    }


@router.patch("/sightings/{sighting_id}")
async def review_sighting(
    sighting_id: str,
    body: SightingReview,
    operator: Operator = Depends(current_operator),
) -> dict:
    """The operator's verdict. The only thing that turns a candidate into a fact.

    A confirmation also teaches the camera graph — see ``dragnet.confirm`` for why a
    rejection deliberately does not unteach it, and why only a direct hop counts.
    """
    sid = as_uuid(sighting_id, "sighting id")
    outcome = await dragnet.confirm(sid, body.confirmed, operator.label)
    if outcome.get("error") == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such sighting")
    if outcome.get("error") == "seed":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The seed sighting is the premise of the pursuit, not a candidate")

    await audit(operator, "pursuit.review", target=sid, confirmed=body.confirmed,
                pursuit_id=outcome.get("pursuit_id"),
                reinforced=outcome.get("graph", {}).get("reinforced"))
    bus.broker.publish(bus.PURSUITS, {
        "kind": "sighting_reviewed",
        "pursuit_id": outcome.get("pursuit_id"), "sighting_id": sid,
        "confirmed": body.confirmed, "camera_id": outcome.get("camera_id"),
        "reviewed_by": operator.label,
    })
    return outcome


@router.patch("/{pursuit_id}")
async def set_status(
    pursuit_id: str,
    body: dict,
    operator: Operator = Depends(current_operator),
) -> dict:
    pid = as_uuid(pursuit_id, "pursuit id")
    new_status = str(body.get("status") or "").lower()
    if new_status not in ("active", "paused", "closed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "status must be active, paused or closed")
    row = await db.fetchrow(
        """
        UPDATE pursuits
           SET status = $2,
               closed_at = CASE WHEN $2 = 'closed' THEN COALESCE(closed_at, NOW()) END,
               title = COALESCE($3, title)
         WHERE id = $1::uuid
        RETURNING id, ref, title, status, closed_at
        """,
        pid, new_status, body.get("title"),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pursuit")
    if new_status in ("paused", "closed"):
        # Predictions are a claim about the near future. Once a pursuit stops being
        # worked, leaving open windows on the map would keep showing operators places
        # to look for a subject nobody is following.
        await db.execute(
            "UPDATE pursuit_predictions SET expired = TRUE "
            "WHERE pursuit_id = $1::uuid AND resolved_by IS NULL AND NOT expired",
            pid,
        )
    await audit(operator, "pursuit.status", target=pid, status=new_status)
    bus.broker.publish(bus.PURSUITS, {
        "kind": "pursuit_status", "pursuit_id": pid, "status": new_status,
    })
    return row


@router.post("/{pursuit_id}/rerun")
async def rerun(
    pursuit_id: str,
    horizon_minutes: Optional[int] = Query(default=None, ge=1, le=720),
    max_hops: Optional[int] = Query(default=None, ge=1, le=6),
    min_evidence_ratio: Optional[float] = Query(default=None, ge=1, le=10_000),
    operator: Operator = Depends(current_operator),
) -> dict:
    """Re-score an existing pursuit.

    Worth doing after two things: a confirmed hop has taught the graph a real transit
    distribution where there was only a haversine estimate, or a batch index has added
    footage the first run could not see. Re-running re-uses the original seed, so the
    premise does not drift between runs.

    Confirmed verdicts survive: ``persist`` upserts candidates and never writes the
    ``confirmed`` column.
    """
    pid = as_uuid(pursuit_id, "pursuit id")
    row = await db.fetchrow(
        "SELECT seed_track_uuid, seed_detection_id, params FROM pursuits WHERE id = $1::uuid",
        pid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pursuit")
    anchor = await dragnet.load_anchor(
        track_uuid=str(row["seed_track_uuid"]) if row["seed_track_uuid"] else None,
        detection_id=str(row["seed_detection_id"]) if row["seed_detection_id"] else None,
    )
    if anchor is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The seed of this pursuit no longer exists")

    params = row["params"] or {}
    result = await dragnet.run(
        anchor,
        horizon_minutes=horizon_minutes or params.get("horizon_minutes"),
        max_hops=max_hops or params.get("max_hops"),
        min_evidence_ratio=(min_evidence_ratio if min_evidence_ratio is not None
                            else params.get("min_evidence_ratio")),
        min_score=params.get("min_score"),
    )
    written = await dragnet.persist(pid, anchor, result)
    await audit(operator, "pursuit.rerun", target=pid,
                candidates=len(result["candidates"]), **written)
    bus.broker.publish(bus.PURSUITS, {
        "kind": "pursuit_rerun", "pursuit_id": pid,
        "candidates": len(result["candidates"]),
    })
    return {
        "pursuit_id": pid,
        "persisted": written,
        "diagnostics": {k: v for k, v in result.items()
                        if k not in ("candidates", "predictions")},
    }
