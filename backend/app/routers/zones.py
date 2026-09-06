"""Features 15–20 configured: the geometry the analytics rules are evaluated against.

Five of the six behavioural detectors are zone-scoped, and the zone is what makes
them mean anything. "Wrong-way" is not a property of a vehicle; it is a heading that
disagrees with the ``direction`` zone drawn on that carriageway. "Restricted-zone
intrusion" is a polygon plus a class list. Nothing here re-implements the rules —
``ml/analytics/rules.py`` owns them — this is the endpoint an operator draws them
with.

Polygons are normalised 0–1, validated in ``schemas.ZoneCreate``. Storing pixels
would silently break every zone the day a department re-encodes a feed at a
different resolution, and the break would look like "the detector stopped working"
rather than "the coordinates moved".

``direction_deg`` is a compass bearing for the *permitted* direction of travel, with
``tolerance_deg`` the accepted spread. A vehicle whose heading differs by more than
the tolerance is what the wrong-way rule fires on.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db
from backend.app.common import as_uuid, audit, resolve_camera
from backend.app.schemas import ZoneCreate
from backend.app.security import Operator, current_operator, require_role

router = APIRouter(prefix="/zones", tags=["zones"])

# Which rule each zone kind feeds, so the UI can say what drawing it will do rather
# than making the operator infer it from a dropdown label.
_RULE_FOR_KIND = {
    "restricted": "zone_intrusion",
    "direction": "wrong_way",
    "crowd": "crowd",
    "abandon_watch": "abandoned_object",
    "count_line": "flow_count",
}


@router.get("")
async def list_zones(
    camera: Optional[str] = None,
    kind: Optional[str] = None,
    active: Optional[bool] = Query(default=None),
    _: Operator = Depends(current_operator),
) -> dict:
    camera_id = (await resolve_camera(camera))["id"] if camera else None
    rows = await db.fetch(
        """
        SELECT z.*, c.external_id AS camera_external_id, c.name AS camera_name, c.district,
               (SELECT COUNT(*) FROM events e WHERE e.zone_id = z.id) AS event_count,
               (SELECT MAX(started_at) FROM events e WHERE e.zone_id = z.id) AS last_event_at
        FROM zones z JOIN cameras c ON c.id = z.camera_id
        WHERE ($1::uuid IS NULL OR z.camera_id = $1)
          AND ($2::text IS NULL OR z.kind = $2)
          AND ($3::boolean IS NULL OR z.active = $3)
        ORDER BY c.external_id, z.name
        """,
        camera_id, kind, active,
    )
    for row in rows:
        row["feeds_rule"] = _RULE_FOR_KIND.get(row["kind"])
    return {"zones": rows, "count": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_role("operator"))])
async def create_zone(body: ZoneCreate, operator: Operator = Depends(current_operator)) -> dict:
    cam = await resolve_camera(body.camera_id)
    if body.kind == "direction" and body.direction_deg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A 'direction' zone needs direction_deg — without it the wrong-way rule "
            "has no reference heading and will never fire",
        )
    row = await db.fetchrow(
        """
        INSERT INTO zones (camera_id, name, kind, polygon, direction_deg, tolerance_deg,
                           params, classes, active, created_by)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9, $10) RETURNING *
        """,
        cam["id"], body.name, body.kind, body.polygon,
        body.direction_deg, body.tolerance_deg, body.params,
        body.classes, body.active, operator.label,
    )
    await audit(operator, "zone.create", target=str(row["id"]),
                camera=cam["external_id"], kind=body.kind, name=body.name)
    return {**row, "feeds_rule": _RULE_FOR_KIND.get(row["kind"])}


@router.patch("/{zone_id}", dependencies=[Depends(require_role("operator"))])
async def update_zone(
    zone_id: str,
    body: dict,
    operator: Operator = Depends(current_operator),
) -> dict:
    zid = as_uuid(zone_id, "zone id")
    allowed = {"name", "polygon", "direction_deg", "tolerance_deg", "params", "classes", "active"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Nothing to update; allowed: {sorted(allowed)}")

    # Every one of these columns is NOT NULL in the DDL, so an explicit null in a PATCH
    # body reaches Postgres as a constraint violation and comes back as a 500. The only
    # nullable one is direction_deg, which is genuinely absent for a non-direction zone.
    # Types are checked here for the same reason: this endpoint takes a raw dict rather
    # than a model, so without this a `{"active": "yes"}` is a DataError from the driver
    # instead of a 400 naming the field.
    _EXPECT = {"name": str, "polygon": list, "tolerance_deg": (int, float),
               "params": dict, "classes": list, "active": bool}
    for column, expected in _EXPECT.items():
        if column not in fields:
            continue
        value = fields[column]
        if value is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{column} cannot be null; omit it to leave it unchanged")
        # bool before int: in Python `True` is an int, and `active` is not a tolerance.
        if isinstance(value, bool) != (expected is bool) or not isinstance(value, expected):
            want = expected if isinstance(expected, type) else expected[0]
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{column} must be a {want.__name__}")
    if "tolerance_deg" in fields and not 0 <= fields["tolerance_deg"] <= 180:
        # Same reason as ZoneCreate: past 180 every heading is inside the permitted arc,
        # which does not loosen the wrong-way rule so much as switch it off.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tolerance_deg must be 0–180")

    if "polygon" in fields:
        poly = fields["polygon"]
        if (not isinstance(poly, list) or len(poly) < 3
                or any(len(p) != 2 or not all(0.0 <= c <= 1.0 for c in p) for p in poly)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "polygon must be ≥3 normalised [x, y] points in 0–1",
            )

    sets, args = [], []
    for column, value in fields.items():
        args.append(value)
        cast = "::jsonb" if column in {"polygon", "params"} else ""
        sets.append(f"{column} = ${len(args)}{cast}")
    args.append(zid)
    row = await db.fetchrow(
        f"UPDATE zones SET {', '.join(sets)} WHERE id = ${len(args)} RETURNING *", *args
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such zone")
    await audit(operator, "zone.update", target=zid, fields=sorted(fields))
    return {**row, "feeds_rule": _RULE_FOR_KIND.get(row["kind"])}


@router.delete("/{zone_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_role("operator"))])
async def delete_zone(
    zone_id: str,
    hard: bool = Query(default=False, description="delete the row instead of deactivating"),
    operator: Operator = Depends(current_operator),
) -> None:
    """Deactivates by default.

    ``events.zone_id`` points here, and an event whose zone has vanished loses the
    geometry that explains why it fired. Deactivating keeps the explanation while
    stopping the rule; ``hard=true`` is available and nulls the references rather
    than cascading, so past events survive their zone.
    """
    zid = as_uuid(zone_id, "zone id")
    existing = await db.fetchrow("SELECT name, kind, camera_id FROM zones WHERE id = $1", zid)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such zone")
    if hard:
        await db.execute("UPDATE events SET zone_id = NULL WHERE zone_id = $1", zid)
        await db.execute("DELETE FROM zones WHERE id = $1", zid)
    else:
        await db.execute("UPDATE zones SET active = FALSE WHERE id = $1", zid)
    await audit(operator, "zone.delete" if hard else "zone.deactivate",
                target=zid, name=existing["name"], kind=existing["kind"])


@router.get("/rules")
async def rule_catalogue(_: Operator = Depends(current_operator)) -> dict:
    """What the analytics layer can detect, and the thresholds it uses.

    Read live from ``ml.analytics.rules.RuleParams`` rather than restated here, so
    the UI cannot show a threshold the engine is not using. That drift is invisible
    until an operator asks why a 90-second dwell did not trigger a 120-second rule
    the screen told them was set to 60.
    """
    from dataclasses import fields as dc_fields

    from ml.analytics.rules import RuleParams

    params = RuleParams()
    detectors = [
        {"rule": "loitering", "feature": 15, "zone_kind": None,
         "description": "A person dwelling in a small area for longer than the threshold."},
        {"rule": "wrong_way", "feature": 16, "zone_kind": "direction",
         "description": "Heading disagrees with the zone's permitted bearing beyond tolerance."},
        {"rule": "accident_candidate", "feature": 17, "zone_kind": None,
         "description": "Two vehicles overlap, then both stop. Scored from approach speed and stationary time."},
        {"rule": "crowd", "feature": 18, "zone_kind": "crowd",
         "description": "More people than the threshold inside the zone for a sustained window."},
        {"rule": "zone_intrusion", "feature": 19, "zone_kind": "restricted",
         "description": "A tracked object of a watched class crosses into a restricted polygon."},
        {"rule": "abandoned_object", "feature": 20, "zone_kind": "abandon_watch",
         "description": "A bag or suitcase stationary with no person within the clearance radius."},
        {"rule": "stopped_vehicle", "feature": None, "zone_kind": None,
         "description": "A vehicle stationary beyond the threshold where it should be moving."},
    ]
    return {
        "detectors": detectors,
        "thresholds": {f.name: getattr(params, f.name) for f in dc_fields(params)},
        "zone_kinds": _RULE_FOR_KIND,
    }
