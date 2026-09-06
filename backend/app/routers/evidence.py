"""Feature 5 — evidence snapshots.

A snapshot is a **manifest with a hash**, not a copy of the video. It records
exactly which detections, tracks and events were selected, the plate reads and
confidences as they stood at that moment, the thumbnail object keys, and a
SHA-256 over the canonical serialisation of all of it.

Why a manifest and not an export
--------------------------------
Model 2's constraint is explicit: no centralised storage of departmental video. An
"evidence export" that pulled clips out of each department's VMS into this service
would violate the one architectural promise the whole design rests on. A manifest
references; it does not copy. The footage stays where its department keeps it, and
the snapshot says precisely which frames to ask for.

Why the hash
------------
The manifest is a claim about what the system observed at a point in time. Rows
change afterwards — a track's consensus plate is revised when the analytics pass
re-runs, an event's status moves to ``false_positive``. Without a hash there is no
way to tell a snapshot that reflects the original observation from one silently
overtaken by later processing. With it, ``/verify`` recomputes and says so.

The hash covers the manifest, not the images: hashing the JPEGs would mean pulling
every object out of MinIO through this process, and the object keys plus MinIO's own
ETags already pin the images. What the hash protects is the *selection* and the
*readings* — which is what gets disputed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
# The digest's normaliser must be *the* encoder the pool writes jsonb with, not a
# lookalike: two functions that agree today are two functions that can drift.
from backend.app.db import _json_default
from backend.app.common import as_uuid, audit
from backend.app.config import settings
from backend.app.schemas import EvidenceCreate
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.evidence")
router = APIRouter(prefix="/evidence", tags=["evidence"])

_REF = "'EVD-' || to_char(NOW(), 'YYYY') || '-' || lpad(nextval('evidence_ref_seq')::text, 4, '0')"


def _canonical(manifest: dict) -> dict:
    """The manifest as Postgres will hand it back — JSON-native, no Python objects.

    This exists because the seal and the verification hash *different things* without
    it. At seal time the manifest holds live ``datetime`` and ``UUID`` objects straight
    out of asyncpg; at verification time it has been through jsonb, so those are
    strings. ``default=str`` renders a datetime as ``2026-09-05 16:10:50+00:00`` while
    the jsonb round-trip renders the same instant as ``2026-09-05T16:10:50+00:00`` —
    one character, a different SHA-256, and every snapshot reporting
    ``manifest_intact: false`` the moment it was sealed. A tamper-evident seal that
    always says "tampered" is worse than none: it cannot distinguish the real thing.

    Normalising *before* both the hash and the INSERT makes the round-trip an identity
    on what is hashed, so the digest is over the stored representation by construction
    rather than by two code paths agreeing to be careful.
    """
    return json.loads(json.dumps(manifest, default=_json_default))


def _digest(manifest: dict) -> str:
    """SHA-256 over a canonical serialisation.

    ``sort_keys`` and a fixed separator set are what make the digest reproducible:
    Python's dict order and default spacing would otherwise change the hash without
    changing the content, and a hash that only the machine that made it can
    reproduce verifies nothing.
    """
    canonical = json.dumps(_canonical(manifest), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _collect(body: EvidenceCreate) -> dict[str, Any]:
    items: dict[str, Any] = {"events": [], "detections": [], "tracks": [], "sightings": []}

    if body.event_ids:
        items["events"] = await db.fetch(
            """
            SELECT e.id, e.event_type, e.severity, e.status, e.label, e.started_at, e.ended_at,
                   e.pts_ms, e.confidence, e.metrics, e.thumbnail_key, e.plate_text,
                   e.track_uuid, e.detection_id, e.source, e.dedupe_key,
                   c.external_id AS camera_external_id, c.name AS camera_name,
                   c.district, c.camera_uid, c.department, c.metadata->>'source_system' AS source_system
            FROM events e JOIN cameras c ON c.id = e.camera_id
            WHERE e.id = ANY($1::uuid[]) ORDER BY e.started_at
            """,
            [as_uuid(i, "event id") for i in body.event_ids],
        )
    if body.detection_ids:
        items["detections"] = await db.fetch(
            """
            SELECT d.id, d.class, d.confidence, d.bbox, d.plate_text, d.plate_confidence,
                   d.dominant_color, d.thumbnail_key, d.ts, d.pts_ms, d.track_uuid,
                   d.frame_w, d.frame_h, d.session_key,
                   c.external_id AS camera_external_id, c.name AS camera_name,
                   c.district, c.camera_uid, c.department, c.metadata->>'source_system' AS source_system
            FROM detections d JOIN cameras c ON c.id = d.camera_id
            WHERE d.id = ANY($1::uuid[]) ORDER BY d.ts
            """,
            [as_uuid(i, "detection id") for i in body.detection_ids],
        )
    if body.track_ids:
        items["tracks"] = await db.fetch(
            """
            SELECT t.id, t.class, t.class_votes, t.dominant_color, t.color_votes,
                   t.plate_text, t.plate_confidence, t.plate_read_count, t.plate_votes,
                   t.first_seen, t.last_seen, t.duration_s, t.detection_count,
                   t.thumbnail_key, t.heading_deg, t.best_detection_id, t.session_key,
                   c.external_id AS camera_external_id, c.name AS camera_name,
                   c.district, c.camera_uid, c.department, c.metadata->>'source_system' AS source_system
            FROM tracks t JOIN cameras c ON c.id = t.camera_id
            WHERE t.id = ANY($1::uuid[]) ORDER BY t.first_seen
            """,
            [as_uuid(i, "track id") for i in body.track_ids],
        )
    if body.pursuit_id:
        pid = as_uuid(body.pursuit_id, "pursuit id")
        items["sightings"] = await db.fetch(
            """
            SELECT s.id, s.pursuit_id, s.ts, s.pts_ms, s.score, s.match_kind,
                   s.score_breakdown, s.confirmed, s.reviewed_by, s.sequence_no,
                   s.thumbnail_key, s.detection_id, s.track_uuid,
                   c.external_id AS camera_external_id, c.name AS camera_name, c.district
            FROM pursuit_sightings s JOIN cameras c ON c.id = s.camera_id
            WHERE s.pursuit_id = $1 ORDER BY s.ts
            """,
            pid,
        )
    return items


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_snapshot(
    body: EvidenceCreate,
    operator: Operator = Depends(current_operator),
) -> dict:
    items = await _collect(body)
    count = sum(len(v) for v in items.values())
    if count == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Nothing referenced — a snapshot with no items would hash to a claim about nothing",
        )

    manifest = {
        "schema": "setu.evidence/1",
        "title": body.title,
        "note": body.note,
        "sealed_by": {"subject": operator.subject, "name": operator.name, "kind": operator.kind},
        # NOW() from the database rather than this process's clock: the timestamps
        # inside the manifest come from Postgres, and a sealed_at from elsewhere
        # could sit outside the range of the evidence it seals.
        "sealed_at": (await db.fetchval("SELECT NOW()")).isoformat(),
        "incident_id": body.incident_id,
        "pursuit_id": body.pursuit_id,
        "counts": {k: len(v) for k, v in items.items()},
        "items": items,
    }
    # The same normalised dict is hashed and stored, so the jsonb round-trip cannot
    # change what a later verification re-derives.
    manifest = _canonical(manifest)
    sha = _digest(manifest)

    row = await db.fetchrow(
        f"""
        INSERT INTO evidence_snapshots (ref, incident_id, title, operator, manifest,
                                        item_count, sha256)
        VALUES ({_REF}, $1, $2, $3, $4::jsonb, $5, $6)
        RETURNING id, ref, incident_id, title, operator, item_count, sha256, created_at
        """,
        as_uuid(body.incident_id, "incident id") if body.incident_id else None,
        body.title, operator.label, manifest, count, sha,
    )
    await audit(operator, "evidence.seal", target=str(row["id"]),
                ref=row["ref"], sha256=sha, items=count)
    return {**row, "counts": manifest["counts"]}


@router.get("")
async def list_snapshots(
    incident_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT ev.id, ev.ref, ev.incident_id, ev.title, ev.operator, ev.item_count,
               ev.sha256, ev.created_at, i.ref AS incident_ref, i.title AS incident_title
        FROM evidence_snapshots ev
        LEFT JOIN incidents i ON i.id = ev.incident_id
        WHERE ($1::uuid IS NULL OR ev.incident_id = $1)
        ORDER BY ev.created_at DESC LIMIT $2
        """,
        as_uuid(incident_id, "incident id") if incident_id else None,
        limit,
    )
    return {"snapshots": rows, "count": len(rows)}


@router.get("/{snapshot_id}")
async def get_snapshot(
    snapshot_id: str,
    with_urls: bool = Query(default=True, description="mint presigned thumbnail URLs"),
    operator: Operator = Depends(current_operator),
) -> dict:
    sid = as_uuid(snapshot_id, "snapshot id")
    row = await db.fetchrow("SELECT * FROM evidence_snapshots WHERE id = $1", sid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such snapshot")

    manifest = row["manifest"]
    if with_urls:
        # URLs are minted on read and never stored in the manifest. A presigned URL
        # expires in minutes; baking one into a sealed document would produce an
        # evidence file whose links are dead by the time anyone opens it, and would
        # put a bearer capability inside a record meant to be shareable.
        for group in manifest.get("items", {}).values():
            if isinstance(group, list):
                await storage.attach_thumbnails(group)

    await audit(operator, "evidence.read", target=sid, ref=row["ref"])
    return {
        "snapshot": {k: v for k, v in row.items() if k != "manifest"},
        "manifest": manifest,
        "presign_expiry_s": settings.presign_expiry_seconds if with_urls else None,
    }


@router.get("/{snapshot_id}/verify")
async def verify(snapshot_id: str, operator: Operator = Depends(current_operator)) -> dict:
    """Recompute the digest, and re-read the referenced rows to report what has moved.

    Two separate questions, answered separately. ``manifest_intact`` is whether the
    sealed document still hashes to its recorded digest — that is tamper detection.
    ``drift`` is whether the live rows still say what the manifest says they said,
    which is *expected* in normal operation: the analytics pass revises a consensus
    plate, an operator marks an event resolved. Conflating the two would either cry
    tampering at routine processing or hide real tampering behind it.
    """
    sid = as_uuid(snapshot_id, "snapshot id")
    row = await db.fetchrow("SELECT * FROM evidence_snapshots WHERE id = $1", sid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such snapshot")

    manifest = row["manifest"]
    recomputed = _digest(manifest)
    intact = recomputed == row["sha256"]

    drift: list[dict] = []
    checks = (
        ("events", "events", ["status", "severity", "label", "plate_text"]),
        ("detections", "detections", ["plate_text", "plate_confidence", "class", "dominant_color"]),
        ("tracks", "tracks", ["plate_text", "plate_confidence", "plate_read_count", "class"]),
    )
    for key, table, columns in checks:
        sealed = manifest.get("items", {}).get(key) or []
        if not sealed:
            continue
        ids = [str(item["id"]) for item in sealed]
        live = {
            str(r["id"]): r
            for r in await db.fetch(
                f"SELECT id, {', '.join(columns)} FROM {table} WHERE id = ANY($1::uuid[])", ids
            )
        }
        for item in sealed:
            now = live.get(str(item["id"]))
            if now is None:
                drift.append({"kind": key, "id": str(item["id"]), "change": "row deleted"})
                continue
            for column in columns:
                if str(item.get(column)) != str(now.get(column)):
                    drift.append({
                        "kind": key, "id": str(item["id"]), "field": column,
                        "sealed": item.get(column), "current": now.get(column),
                    })

    await audit(operator, "evidence.verify", target=sid,
                ref=row["ref"], intact=intact, drift=len(drift))
    return {
        "ref": row["ref"],
        "sealed_at": manifest.get("sealed_at"),
        "sealed_by": manifest.get("sealed_by"),
        "manifest_intact": intact,
        "recorded_sha256": row["sha256"],
        "recomputed_sha256": recomputed,
        "item_count": row["item_count"],
        "drift_count": len(drift),
        "drift": drift[:200],
    }
