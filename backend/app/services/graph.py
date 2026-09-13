"""The camera transition graph — what turns 30 independent cameras into a network.

Every other feature in this service treats a camera as an isolated stream. Feature
9 (camera-to-camera investigation) and the reconstruction engine cannot: "where did
it go next" is a question about the *edges*, and nothing in the schema knew them.

Two kinds of edge, deliberately kept distinguishable rather than blended into one
opaque weight:

``geo``
    Derived from coordinates. Always available — it needs no history at all, which
    matters because a newly-added camera has no observations and would otherwise be
    invisible to the graph on the night it is installed. Weak: a straight line
    between two points says nothing about whether a road connects them, and two
    cameras 400 m apart across a river are neighbours to a haversine and unreachable
    to a driver.

``plate``
    Derived from the same plate being read at A and then at B. This is a real
    observation of a real route, with a real transit-time distribution. Strong, but
    only exists where ANPR fired — and it is built from ``tracks``, never from raw
    detections, because a track's plate is the folded answer over every frame the
    vehicle was visible for and a single frame's read is not an identification.

``confirmed``
    An operator said so. Never overwritten by either of the above.

The transit distribution is the part that earns its keep. A prediction of "somewhere
downstream, eventually" is useless in a control room; ``p10``–``p90`` gives a window
to actually watch, and an arrival outside the window is evidence *against* the match
rather than a coincidence to be explained away.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app import db
from backend.app.config import settings

log = logging.getLogger("setu.api.graph")

# Distance in metres between two lat/lng pairs. Written out rather than imported
# because it is used inside SQL as well as Python, and one of the two would drift.
_HAVERSINE_M = """
    6371000 * 2 * asin(sqrt(
        power(sin(radians({b}.lat - {a}.lat) / 2), 2)
      + cos(radians({a}.lat)) * cos(radians({b}.lat))
      * power(sin(radians({b}.lng - {a}.lng) / 2), 2)
    ))
"""


def transit_bounds(distance_m: Optional[float]) -> tuple[float, float, float]:
    """(p10, median, p90) seconds for a distance, from assumed speeds.

    The fallback for an edge with no observed transits. Ordered as fast/typical/slow,
    which means the *fastest* speed produces the *smallest* time — inverting that is
    the easy mistake, and it produces windows that close before they open.
    """
    if not distance_m or distance_m <= 0:
        return (5.0, 30.0, 120.0)
    road_m = distance_m * settings.graph_detour_factor
    fast = road_m / (settings.graph_free_speed_kmh * 1000 / 3600)
    typical = road_m / (settings.graph_urban_speed_kmh * 1000 / 3600)
    slow = road_m / (settings.graph_congested_speed_kmh * 1000 / 3600)
    return (fast, typical, slow)


def band_ratios() -> tuple[float, float]:
    """(fast, slow) multipliers that turn a *typical* transit into a window.

    Derived from the same three speeds as :func:`transit_bounds` rather than picked:
    a median transit already encodes the real road length and the real signals on it,
    so the only question left is how much faster or slower than typical a vehicle can
    be, and the config already answers that. At the defaults — 25 km/h typical, 55
    free-flowing, 10 congested — that is 0.45x to 2.5x, which is the same ~200%-of-median
    band the geographic edges carry. Keeping it derived means retuning the city's speed
    assumptions moves every window together instead of leaving hardcoded pairs behind.
    """
    urban = max(settings.graph_urban_speed_kmh, 1e-6)
    return (urban / max(settings.graph_free_speed_kmh, 1e-6),
            urban / max(settings.graph_congested_speed_kmh, 1e-6))


def _rows(tag: str) -> int:
    """Row count out of an asyncpg command tag (``"INSERT 0 64"``)."""
    try:
        return int(str(tag).rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


async def rebuild(include_geo: bool = True, include_plates: bool = True) -> dict:
    """Recompute the graph. Idempotent; ``confirmed`` edges survive untouched.

    Ordering matters: geography first as the floor, then observed transitions on top,
    so a pair that has both ends up described by what was actually seen rather than
    by what the coordinates guessed.
    """
    stats = {"geo_edges": 0, "plate_edges": 0, "confirmed_preserved": 0}
    stats["confirmed_preserved"] = await db.fetchval(
        "SELECT COUNT(*) FROM camera_links WHERE evidence = 'confirmed'"
    )

    if include_geo:
        # Both directions are inserted: the query emits each ordered pair once, and
        # a one-way graph would make "where did it come from" unanswerable.
        haversine = _HAVERSINE_M.format(a="a", b="b")
        tag_geo = await db.execute(
            f"""
            WITH pairs AS (
                SELECT a.id AS src, b.id AS dst, {haversine} AS distance_m
                FROM cameras a
                JOIN cameras b ON b.id <> a.id
                WHERE a.lat IS NOT NULL AND a.lng IS NOT NULL
                  AND b.lat IS NOT NULL AND b.lng IS NOT NULL
                  AND a.status <> 'retired' AND b.status <> 'retired'
            ),
            near AS (
                SELECT * FROM pairs WHERE distance_m <= $1
            ),
            weighted AS (
                -- Inverse distance, normalised per source. Not a real routing
                -- probability and not presented as one: it is a prior that says
                -- "closer is likelier", which is the most geography can support.
                SELECT src, dst, distance_m,
                       (1.0 / GREATEST(distance_m, 50)) /
                       SUM(1.0 / GREATEST(distance_m, 50)) OVER (PARTITION BY src)
                           AS probability
                FROM near
            )
            INSERT INTO camera_links (src_camera_id, dst_camera_id, distance_m,
                                      p10_transit_s, median_transit_s, p90_transit_s,
                                      probability, observations, evidence, updated_at)
            SELECT src, dst, distance_m,
                   (distance_m * $2) / ($3 * 1000 / 3600.0),
                   (distance_m * $2) / ($4 * 1000 / 3600.0),
                   (distance_m * $2) / ($5 * 1000 / 3600.0),
                   probability, 0, 'geo', NOW()
            FROM weighted
            ON CONFLICT (src_camera_id, dst_camera_id) DO UPDATE SET
                distance_m = EXCLUDED.distance_m,
                updated_at = NOW(),
                -- Geography must not clobber a stronger edge. Only the distance is
                -- refreshed when the existing row was built from observations.
                p10_transit_s    = CASE WHEN camera_links.evidence = 'geo'
                                        THEN EXCLUDED.p10_transit_s
                                        ELSE camera_links.p10_transit_s END,
                median_transit_s = CASE WHEN camera_links.evidence = 'geo'
                                        THEN EXCLUDED.median_transit_s
                                        ELSE camera_links.median_transit_s END,
                p90_transit_s    = CASE WHEN camera_links.evidence = 'geo'
                                        THEN EXCLUDED.p90_transit_s
                                        ELSE camera_links.p90_transit_s END,
                probability      = CASE WHEN camera_links.evidence = 'geo'
                                        THEN EXCLUDED.probability
                                        ELSE camera_links.probability END
            """,
            settings.graph_max_link_distance_m,
            settings.graph_detour_factor,
            settings.graph_free_speed_kmh,
            settings.graph_urban_speed_kmh,
            settings.graph_congested_speed_kmh,
        )
        stats["geo_rows_written"] = _rows(tag_geo)

    if include_plates:
        tag_plate = await db.execute(
            """
            WITH sightings AS (
                -- Track-level plates only. A single frame's read is not an
                -- identification; building edges from raw detections would
                -- manufacture transitions between cameras a vehicle never
                -- visited, and the graph would then confidently predict them.
                SELECT t.plate_text, t.camera_id, t.first_seen, t.last_seen
                FROM tracks t
                WHERE t.plate_text IS NOT NULL
                  AND length(t.plate_text) >= 6
                  AND COALESCE(t.plate_confidence, 0) >= 0.35
            ),
            seq AS (
                SELECT plate_text,
                       camera_id                        AS src,
                       last_seen                        AS depart,
                       LEAD(camera_id)   OVER w         AS dst,
                       LEAD(first_seen)  OVER w         AS arrive
                FROM sightings
                WINDOW w AS (PARTITION BY plate_text ORDER BY first_seen)
            ),
            hops AS (
                -- Consecutive only. A plate seen at A, then B, then C must not
                -- produce an A→C edge: the vehicle passed through B, and an edge
                -- claiming a direct route would corrupt every transit estimate
                -- built on top of it.
                SELECT src, dst, EXTRACT(EPOCH FROM (arrive - depart)) AS transit_s
                FROM seq
                WHERE dst IS NOT NULL AND dst <> src
                  AND arrive >= depart
                  AND EXTRACT(EPOCH FROM (arrive - depart)) BETWEEN 1 AND $1
            ),
            agg AS (
                SELECT src, dst, COUNT(*)::int AS n,
                       percentile_cont(0.1) WITHIN GROUP (ORDER BY transit_s) AS p10,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY transit_s) AS med,
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY transit_s) AS p90
                FROM hops GROUP BY src, dst
            ),
            scored AS (
                SELECT *, n::real / SUM(n) OVER (PARTITION BY src) AS probability
                FROM agg WHERE n >= $2
            ),
            shrunk AS (
                -- The median is taken as observed; the *spread* is not. Two sightings
                -- that happened to transit in 756 s and 789 s say the route takes about
                -- 13 minutes — they do not say every vehicle takes between 756 and 789
                -- seconds, which is what percentile_cont returns when handed two rows.
                -- Blending toward the speed-derived band with weight n/(n+k) keeps the
                -- gain observations really provide and drops the precision they do not.
                -- See config.graph_transit_shrinkage for the measurement that forced it.
                SELECT src, dst, n, med, probability,
                       (n::float8 / (n + $3::float8)) AS w, p10, p90
                FROM scored
            )
            INSERT INTO camera_links (src_camera_id, dst_camera_id, observations,
                                      p10_transit_s, median_transit_s, p90_transit_s,
                                      probability, evidence, updated_at)
            SELECT src, dst, n,
                   w * p10 + (1 - w) * (med * $4::float8),
                   med,
                   w * p90 + (1 - w) * (med * $5::float8),
                   probability, 'plate', NOW()
            FROM shrunk
            ON CONFLICT (src_camera_id, dst_camera_id) DO UPDATE SET
                observations     = EXCLUDED.observations,
                p10_transit_s    = EXCLUDED.p10_transit_s,
                median_transit_s = EXCLUDED.median_transit_s,
                p90_transit_s    = EXCLUDED.p90_transit_s,
                probability      = EXCLUDED.probability,
                evidence         = CASE WHEN camera_links.evidence = 'confirmed'
                                        THEN 'confirmed' ELSE 'plate' END,
                updated_at       = NOW()
            """,
            settings.graph_transit_horizon_s,
            settings.graph_min_observations,
            settings.graph_transit_shrinkage,
            *band_ratios(),
        )
        stats["plate_rows_written"] = _rows(tag_plate)

    # Counted after both passes, not between them. The plate pass upgrades edges the
    # geo pass created, so a count taken mid-rebuild reports edges that no longer
    # exist under that label and the three numbers do not add up to the total.
    final = await db.fetchrow(
        """
        SELECT COUNT(*)                                      AS total,
               COUNT(*) FILTER (WHERE evidence = 'geo')       AS geo,
               COUNT(*) FILTER (WHERE evidence = 'plate')     AS plate,
               COUNT(*) FILTER (WHERE evidence = 'confirmed') AS confirmed
        FROM camera_links
        """
    )
    stats.update({
        "geo_edges": final["geo"], "plate_edges": final["plate"],
        "confirmed_edges": final["confirmed"], "total_edges": final["total"],
    })
    log.info("camera graph rebuilt: %s", stats)
    return stats


async def edges(active_only: bool = True) -> list[dict]:
    """The whole graph, in one query.

    30 cameras is at most 870 edges. Loading them once and walking them in Python
    beats a recursive CTE that has to be re-planned per expansion, and it keeps the
    beam-search pruning below readable rather than expressed in SQL.
    """
    return await db.fetch(
        f"""
        SELECT l.src_camera_id, l.dst_camera_id, l.observations, l.median_transit_s,
               l.p10_transit_s, l.p90_transit_s, l.distance_m, l.probability, l.evidence,
               d.external_id AS dst_external_id, d.name AS dst_name, d.district AS dst_district
        FROM camera_links l
        JOIN cameras d ON d.id = l.dst_camera_id
        {"WHERE d.status <> 'retired'" if active_only else ""}
        """
    )


async def neighbours(camera_id: str, limit: int = 12) -> list[dict]:
    """Outbound edges from one camera, strongest first.

    Ordered by evidence class before probability: an edge backed by two observed
    plate transitions is worth more to an investigator than a geographic guess with
    a higher normalised weight, and sorting on probability alone buries it.
    """
    return await db.fetch(
        """
        SELECT l.dst_camera_id AS camera_id, c.external_id, c.name, c.district, c.area,
               c.lat, c.lng, c.status,
               l.observations, l.median_transit_s, l.p10_transit_s, l.p90_transit_s,
               l.distance_m, l.probability, l.evidence, l.updated_at,
               r.observations AS reverse_observations, r.evidence AS reverse_evidence
        FROM camera_links l
        JOIN cameras c ON c.id = l.dst_camera_id
        LEFT JOIN camera_links r
               ON r.src_camera_id = l.dst_camera_id AND r.dst_camera_id = l.src_camera_id
        WHERE l.src_camera_id = $1 AND c.status <> 'retired'
        ORDER BY CASE l.evidence WHEN 'confirmed' THEN 0 WHEN 'plate' THEN 1 ELSE 2 END,
                 l.probability DESC
        LIMIT $2
        """,
        camera_id, limit,
    )


# Beam width for the expansion below. A 5 km radius in a city centre makes the geo
# graph near-complete, so an exhaustive 4-hop walk over 30 cameras is ~700k paths —
# almost all of them with a cumulative probability far below anything worth watching.
# Keeping the best 64 frontier states per hop bounds the work at a few thousand
# expansions and loses only branches that were already being discarded.
_BEAM = 64


async def expand(
    origin_camera_id: str,
    depart_ts,
    horizon_s: float,
    max_hops: int = 3,
    min_probability: float = 0.01,
    edge_rows: Optional[list[dict]] = None,
) -> list[dict]:
    """Where a subject leaving ``origin`` at ``depart_ts`` could be, and when.

    Returns one entry per (camera, hop count) with an ETA window and a probability.
    Two paths reaching the same camera in the same number of hops are alternative
    routes and therefore mutually exclusive, so their probabilities add and their
    windows union — merging them into one entry rather than reporting the same camera
    twice with two half-probabilities, which reads to an operator as a weaker lead
    than it is.
    """
    from datetime import timedelta

    rows = edge_rows if edge_rows is not None else await edges()
    out: dict[str, list[dict]] = {}
    for e in rows:
        out.setdefault(str(e["src_camera_id"]), []).append(e)

    deadline = depart_ts + timedelta(seconds=horizon_s)
    merged: dict[tuple[str, int], dict] = {}
    frontier = [{
        "camera_id": str(origin_camera_id), "ts": depart_ts, "probability": 1.0,
        "hops": 0, "via": [], "visited": {str(origin_camera_id)},
    }]

    for hop in range(1, max_hops + 1):
        nxt: list[dict] = []
        for state in frontier:
            for e in out.get(state["camera_id"], []):
                dst = str(e["dst_camera_id"])
                if dst in state["visited"]:
                    # A route that returns to a camera it already passed is possible
                    # in reality and unhelpful as a prediction: it doubles the search
                    # space to tell an operator to re-watch a camera they have already
                    # been given. Circular leads are left to the operator's own next hop.
                    continue
                p10, med, p90 = (e["p10_transit_s"], e["median_transit_s"], e["p90_transit_s"])
                if med is None:
                    p10, med, p90 = transit_bounds(e["distance_m"])
                p10 = p10 if p10 is not None else med * 0.5
                p90 = p90 if p90 is not None else med * 2.0
                eta_from = state["ts"] + timedelta(seconds=float(p10))
                eta_to = state["ts"] + timedelta(seconds=float(p90))
                if eta_from > deadline:
                    continue
                prob = state["probability"] * float(e["probability"] or 0.0)
                if prob < min_probability:
                    continue

                eta_peak = state["ts"] + timedelta(seconds=float(med))
                key = (dst, hop)
                via = state["via"] + [state["camera_id"]]
                slot = merged.get(key)
                if slot is None:
                    slot = merged[key] = {
                        "camera_id": dst, "hops": hop,
                        "external_id": e.get("dst_external_id"), "name": e.get("dst_name"),
                        "district": e.get("dst_district"),
                        "eta_from": eta_from, "eta_to": min(eta_to, deadline),
                        "probability": 0.0, "observations": e["observations"],
                        "_best": -1.0,
                    }
                else:
                    slot["eta_from"] = min(slot["eta_from"], eta_from)
                    slot["eta_to"] = max(slot["eta_to"], min(eta_to, deadline))
                slot["probability"] = min(1.0, slot["probability"] + prob)
                if prob > slot["_best"]:
                    # The route an operator is shown is the likeliest one that
                    # reaches this camera, not whichever was walked first.
                    slot["_best"] = prob
                    slot["from_camera_id"] = state["camera_id"]
                    slot["via"] = via[1:]
                    slot["evidence"] = e["evidence"]
                    slot["eta_peak"] = eta_peak

                # The next hop departs from this path's own peak arrival, not from
                # the merged slot's: two routes into one camera arrive at different
                # times, and continuing both from one of them shifts the other's
                # whole downstream window.
                nxt.append({
                    "camera_id": dst, "ts": eta_peak, "probability": prob,
                    "hops": hop, "via": via, "visited": state["visited"] | {dst},
                })

        nxt.sort(key=lambda s: s["probability"], reverse=True)
        frontier = nxt[:_BEAM]
        if not frontier:
            break

    results = []
    for entry in merged.values():
        entry.pop("_best", None)
        results.append(entry)
    results.sort(key=lambda r: (-r["probability"], r["hops"]))
    return results
