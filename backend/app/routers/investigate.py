"""Feature 9 — camera-to-camera investigation.

Every other endpoint in this service treats a camera as an isolated stream. This one
treats the estate as a graph, which is the only way to answer the question an
investigator actually has: *where did it go next?*

Two modes, deliberately distinct:

**Manual walk** (``/next-hop``) — the operator drives. Pick a vehicle, ask which
neighbouring cameras it could have reached, get the candidates at each with their
scores, choose one, repeat. Nothing is saved; the operator is thinking, not filing.

**Reconstruction** (``routers/dragnet.py``) — the engine walks several hops on its own
and files the result as a pursuit.

Both use the same scorer in ``services/dragnet.py``, so a hop scored by hand and a hop
scored by the engine give the same number. Two implementations of "is this the same
vehicle" would eventually disagree, and the operator would have no way to know which
one to believe.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db, storage
from backend.app.common import as_uuid, audit, resolve_camera
from backend.app.schemas import CameraLinkUpsert, NextHop
from backend.app.security import Operator, current_operator, require_role
from backend.app.services import dragnet, graph

log = logging.getLogger("setu.api.investigate")
router = APIRouter(prefix="/investigate", tags=["investigate"])


@router.get("/graph")
async def get_graph(
    district: str | None = None,
    min_probability: float = Query(default=0.0, ge=0, le=1),
    evidence: str | None = Query(default=None, description="geo | plate | confirmed"),
    _: Operator = Depends(current_operator),
) -> dict:
    """Nodes and edges, for a map or a force layout.

    Returned as two flat lists rather than nested adjacency: the client renders edges
    as a separate layer from markers, and nesting them would mean either sending each
    edge twice or making the renderer reassemble them.
    """
    nodes = await db.fetch(
        """
        SELECT c.id, c.external_id, c.name, c.district, c.area, c.lat, c.lng, c.status,
               c.metadata->>'source_system' AS source_system,
               COALESCE(ci.track_count, 0) AS track_count,
               COALESCE(ci.event_count, 0) AS event_count
        FROM cameras c
        LEFT JOIN camera_index ci ON ci.id = c.id
        WHERE c.status <> 'retired' AND ($1::text IS NULL OR c.district = $1)
        ORDER BY c.external_id
        """,
        district,
    )
    ids = {str(n["id"]) for n in nodes}
    edges = await db.fetch(
        """
        SELECT l.src_camera_id, l.dst_camera_id, l.observations, l.median_transit_s,
               l.p10_transit_s, l.p90_transit_s, l.distance_m, l.probability,
               l.evidence, l.updated_at
        FROM camera_links l
        WHERE l.probability >= $1 AND ($2::text IS NULL OR l.evidence = $2)
        ORDER BY l.probability DESC
        """,
        min_probability, evidence,
    )
    # Filtering here rather than in SQL: the district filter applies to nodes, and an
    # edge is only meaningful if both of its endpoints survived it.
    edges = [
        e for e in edges
        if str(e["src_camera_id"]) in ids and str(e["dst_camera_id"]) in ids
    ]
    by_evidence: dict[str, int] = {}
    for e in edges:
        by_evidence[e["evidence"]] = by_evidence.get(e["evidence"], 0) + 1
    return {
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "cameras": len(nodes), "edges": len(edges), "by_evidence": by_evidence,
            "built": bool(edges),
        },
    }


@router.get("/neighbours/{camera_ref}")
async def get_neighbours(
    camera_ref: str,
    limit: int = Query(default=12, ge=1, le=60),
    _: Operator = Depends(current_operator),
) -> dict:
    camera = await resolve_camera(camera_ref)
    rows = await graph.neighbours(str(camera["id"]), limit)
    for r in rows:
        # A geographic edge has no observed transit distribution; the bounds shown are
        # derived from distance and assumed speeds, and saying which is which stops an
        # operator reading a guess as a measurement.
        r["transit_source"] = "observed" if (r["observations"] or 0) > 0 else "estimated"
    return {"camera": camera, "neighbours": rows, "count": len(rows)}


@router.post("/graph/rebuild")
async def rebuild_graph(
    geo: bool = Query(default=True),
    plates: bool = Query(default=True),
    operator: Operator = Depends(require_role("admin")),
) -> dict:
    """Recompute the graph from geography and observed plate transitions.

    Safe to run repeatedly and cheap enough to run after a batch index — it is two
    aggregate queries over ``cameras`` and ``tracks``. Operator-confirmed edges are
    preserved: they are the only ones backed by a person having looked.
    """
    stats = await graph.rebuild(include_geo=geo, include_plates=plates)
    await audit(operator, "graph.rebuild", **stats)
    return stats


@router.put("/links")
async def upsert_link(
    body: CameraLinkUpsert,
    operator: Operator = Depends(require_role("admin")),
) -> dict:
    """Record a route an operator knows about that the data has not shown.

    Marked ``confirmed``, which makes it immune to being overwritten by the geographic
    pass — someone who knows the junction is a better authority on whether a road
    connects than a haversine is.

    The operator states a typical transit; the p10/p90 window around it comes from
    ``graph.band_ratios()``, the same speed assumptions that widen a thinly-observed
    plate edge. It used to be a hardcoded 0.6x/1.8x here, which meant an operator-
    confirmed route and a measured one disagreed about how wide a window is for no
    reason either of them could see.
    """
    src, dst = as_uuid(body.src_camera_id, "src"), as_uuid(body.dst_camera_id, "dst")
    if src == dst:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A camera does not link to itself")
    row = await db.fetchrow(
        """
        INSERT INTO camera_links (src_camera_id, dst_camera_id, median_transit_s,
                                  p10_transit_s, p90_transit_s, distance_m,
                                  probability, observations, evidence, updated_at)
        -- $3 is cast at every use: without it Postgres deduces `real` from the
        -- median_transit_s column and `double precision` from the multiplication, and
        -- refuses the statement with "inconsistent types deduced for parameter $3".
        VALUES ($1, $2, $3::float8, $3::float8 * $5::float8, $3::float8 * $6::float8,
                $4, 0.25, 0, 'confirmed', NOW())
        ON CONFLICT (src_camera_id, dst_camera_id) DO UPDATE SET
            median_transit_s = EXCLUDED.median_transit_s,
            p10_transit_s = EXCLUDED.p10_transit_s,
            p90_transit_s = EXCLUDED.p90_transit_s,
            distance_m = COALESCE(EXCLUDED.distance_m, camera_links.distance_m),
            evidence = 'confirmed', updated_at = NOW()
        RETURNING *
        """,
        src, dst, float(body.median_transit_s), body.distance_m, *graph.band_ratios(),
    )
    await audit(operator, "graph.link", target=f"{src}->{dst}",
                median_transit_s=body.median_transit_s)
    return {"link": row}


@router.post("/next-hop")
async def next_hop(body: NextHop, operator: Operator = Depends(current_operator)) -> dict:
    """One hop, scored. The manual half of feature 9.

    Answers "which of these cameras has my vehicle in the next few minutes, and how
    strongly". Returns per-camera groups rather than one flat ranking, because the
    operator's next action is to open a camera, and a flat list interleaved across six
    cameras makes them hunt for which one to click.
    """
    anchor = await dragnet.load_anchor(
        track_uuid=as_uuid(body.track_uuid, "track id") if body.track_uuid else None,
        detection_id=as_uuid(body.detection_id, "detection id") if body.detection_id else None,
    )
    if anchor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Give a track_uuid or detection_id that exists")

    horizon_s = float(body.horizon_minutes * 60)
    if body.to_camera:
        target = await resolve_camera(body.to_camera)
        candidates_for = await graph.neighbours(anchor.camera_id, limit=200)
        edges = [e for e in candidates_for if str(e["camera_id"]) == str(target["id"])]
        if not edges:
            # No edge is not the same as no answer. The operator asked about a specific
            # camera; refusing because the graph has not connected them yet would make
            # the tool useless exactly when it is most needed — an unmapped route.
            edges = [{
                "camera_id": str(target["id"]), "external_id": target["external_id"],
                "name": target.get("name"), "district": target.get("district"),
                "probability": 0.05, "evidence": "unlinked", "observations": 0,
                "median_transit_s": None, "p10_transit_s": None, "p90_transit_s": None,
                "distance_m": None,
            }]
    else:
        edges = await graph.neighbours(anchor.camera_id, limit=body.max_cameras)

    groups = []
    for edge in edges:
        p10, med, p90 = (edge.get("p10_transit_s"), edge.get("median_transit_s"),
                         edge.get("p90_transit_s"))
        if med is None:
            p10, med, p90 = graph.transit_bounds(edge.get("distance_m"))
        eta_from = anchor.ts + timedelta(seconds=float(p10 or med * 0.5))
        eta_to = anchor.ts + timedelta(seconds=min(float(p90 or med * 2.0), horizon_s))
        scored = await dragnet.hop(
            anchor,
            {**edge, "hops": 1, "from_camera_id": anchor.camera_id,
             "p10_transit_s": p10, "median_transit_s": med, "p90_transit_s": p90},
            eta_from=eta_from - timedelta(seconds=30),
            eta_to=eta_to + timedelta(seconds=max((eta_to - eta_from).total_seconds() * 0.5, 30)),
            horizon_s=horizon_s,
            exclude=[anchor.track_uuid] if anchor.track_uuid else None,
        )
        kept = [c for c in scored if c["evidence_ratio"] >= body.min_evidence_ratio]
        kept = kept[: body.limit_per_camera]
        await storage.attach_thumbnails(kept)
        groups.append({
            "camera": {
                "id": str(edge["camera_id"]), "external_id": edge.get("external_id"),
                "name": edge.get("name"), "district": edge.get("district"),
                "lat": edge.get("lat"), "lng": edge.get("lng"),
            },
            "corridor": {
                "probability": edge.get("probability"), "evidence": edge.get("evidence"),
                "observations": edge.get("observations"),
                "transit_source": "observed" if (edge.get("observations") or 0) > 0 else "estimated",
                "eta_from": eta_from, "eta_to": eta_to,
                "median_transit_s": round(float(med), 1) if med else None,
            },
            "examined": len(scored),
            "candidates": kept,
        })

    groups.sort(key=lambda g: -(g["candidates"][0]["evidence_ratio"] if g["candidates"] else 0))
    return {
        "anchor": {
            "kind": anchor.kind, "track_uuid": anchor.track_uuid,
            "detection_id": anchor.detection_id, "camera_id": anchor.camera_id,
            "camera_name": anchor.camera_name, "ts": anchor.ts, "class": anchor.vclass,
            "color": anchor.color, "plate_text": anchor.plate,
            "plate_confidence": anchor.plate_confidence,
            "thumbnail_url": await storage.presign(anchor.thumbnail_key),
            "has_embedding": anchor.has_embedding,
        },
        "cameras": groups,
        "totals": {
            "cameras": len(groups),
            "candidates": sum(len(g["candidates"]) for g in groups),
            "examined": sum(g["examined"] for g in groups),
        },
    }


@router.get("/plate/{plate}")
async def plate_corridor(
    plate: str,
    hours: float = Query(default=24.0, gt=0, le=720),
    _: Operator = Depends(current_operator),
) -> dict:
    """Every camera that read this plate, in order, with the hops between them.

    The easy case of camera-to-camera investigation, and worth having separately: when
    ANPR got the plate at both ends there is nothing to infer. Consensus plates only —
    a corridor assembled from single-frame reads at 17.6% accuracy would be a route
    through cameras the vehicle never passed.

    Confusion-aware matching is applied on top of the exact lookup, so a hop where one
    camera read ``GJ0IAB1234`` still appears — marked as the misread it is.
    """
    normalised = "".join(ch for ch in plate.upper() if ch.isalnum())
    rows = await db.fetch(
        """
        SELECT t.id AS track_uuid, t.camera_id, t.class, t.dominant_color, t.plate_text,
               t.plate_confidence, t.plate_read_count, t.first_seen, t.last_seen,
               t.thumbnail_key, t.best_detection_id,
               c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng,
               similarity(t.plate_text, $1) AS trigram
        FROM tracks t JOIN cameras c ON c.id = t.camera_id
        WHERE t.plate_text IS NOT NULL
          AND t.first_seen >= NOW() - ($2 || ' hours')::interval
          AND (t.plate_text = $1 OR similarity(t.plate_text, $1) >= 0.5)
        ORDER BY t.first_seen
        """,
        normalised, str(hours),
    )
    sightings = []
    for r in rows:
        sim = dragnet.plate_similarity(normalised, r["plate_text"])
        if r["plate_text"] != normalised and sim < 0.85:
            continue
        r["plate_similarity"] = round(sim, 3)
        r["read_match"] = "exact" if r["plate_text"] == normalised else "confusion"
        sightings.append(r)

    await storage.attach_thumbnails(sightings)
    hops = []
    for prev, curr in zip(sightings, sightings[1:]):
        if str(prev["camera_id"]) == str(curr["camera_id"]):
            continue
        transit_s = (curr["first_seen"] - prev["last_seen"]).total_seconds()
        edge = await db.fetchrow(
            """
            SELECT observations, median_transit_s, p10_transit_s, p90_transit_s,
                   distance_m, probability, evidence
            FROM camera_links WHERE src_camera_id = $1 AND dst_camera_id = $2
            """,
            prev["camera_id"], curr["camera_id"],
        )
        implied_kmh = None
        if edge and edge["distance_m"] and transit_s > 0:
            implied_kmh = round(edge["distance_m"] / transit_s * 3.6, 1)
        hops.append({
            "from": {"camera_id": str(prev["camera_id"]),
                     "external_id": prev["camera_external_id"], "ts": prev["last_seen"]},
            "to": {"camera_id": str(curr["camera_id"]),
                   "external_id": curr["camera_external_id"], "ts": curr["first_seen"]},
            "transit_s": round(transit_s, 1),
            "implied_speed_kmh": implied_kmh,
            "link": dict(edge) if edge else None,
            # An implied speed far above what the road allows means the two reads are
            # not the same vehicle — which is exactly the check a plate corridor needs,
            # because two vehicles with confusable plates produce a beautiful-looking
            # route through the city.
            "plausible": (implied_kmh is None or implied_kmh <= 120),
        })
    return {
        "plate": normalised,
        "sightings": sightings,
        "hops": hops,
        "cameras": len({str(s["camera_id"]) for s in sightings}),
        "span_s": (
            round((sightings[-1]["last_seen"] - sightings[0]["first_seen"]).total_seconds(), 1)
            if len(sightings) > 1 else 0
        ),
    }
