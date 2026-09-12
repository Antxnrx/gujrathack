"""Derive zones from where traffic actually went, rather than drawing them by hand.

Why derive them
---------------
Four of the seven detectors need zones and there were none, so ``wrong_way`` and
``zone_intrusion`` had never fired on real data. Hand-drawing polygons for 30
cameras means opening 30 feeds and guessing where the carriageway is — slow, and
wrong wherever the guess is off by a lane.

The tracks already answer the question. Every vehicle that crossed a camera left
a path, so the union of those paths *is* the carriageway, measured rather than
estimated. The same data gives the permitted heading.

Where a permitted direction exists, and where it does not
---------------------------------------------------------
The first version made one direction zone per heading cluster, and it was wrong
in a way only the numbers showed: 180 wrong-way events from 509 moving tracks.
At a junction the clusters' polygons cover the same tarmac, so a vehicle
correctly following one approach sits inside another approach's zone and is
judged against a heading that never applied to it.

"The permitted direction here" is only meaningful on some cameras, so that is
tested rather than assumed:

* **one-way** — headings concentrate (circular R >= 0.55). The whole carriageway
  carries one permitted heading.
* **two-way** — two opposing flows (>= 130° apart) that occupy *different
  ground*. A real divided road separates its directions in space, so each side
  gets its own zone and a vehicle against the flow on that side is genuinely
  wrong-way.
* **neither** — a junction, roundabout or forecourt. No direction zone is
  created and wrong-way is simply not claimed there. Six of seven cameras with
  enough traffic fall here, which is an honest description of this estate.

What gets created per camera
----------------------------
* ``direction`` zones only where the above holds — scoped to vehicle classes, so
  a pedestrian crossing against traffic is not recorded as a traffic offence.
* one ``restricted`` zone over the live carriageway, scoped to ``person``, with
  a dwell gate. Crossing the road is not an anomaly in Gujarat traffic — the
  ungated rule produced 223 events, which is a description of normal behaviour.
  *Standing* in live traffic is the hazard, and that is what the gate isolates.

Both are marked ``created_by='derived'`` so an operator can tell them from
hand-drawn ones and correct them in the Zones editor, which remains the
authority.

Usage:
    python -m scripts.derive_zones --dry-run
    python -m scripts.derive_zones
    python -m scripts.derive_zones --replace        # drop previous derived zones
"""
from __future__ import annotations

import argparse
import json
import logging
import math

import cv2
import numpy as np
import psycopg2
import psycopg2.extras

log = logging.getLogger("setu.zones")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
VEHICLES = ("car", "truck", "bus", "motorcycle", "auto_rickshaw", "tractor")

# A flow needs this many tracks before it is a flow rather than a few vehicles
# that happened to turn. Below it the circular mean is noise and the polygon is
# the hull of a handful of points.
MIN_TRACKS_PER_FLOW = 12
# Headings are binned before clustering. 45° bins match how junction approaches
# actually separate, and are coarse enough that lane changes stay in one bin.
HEADING_BIN_DEG = 45
# A track that barely moved has a meaningless heading.
MIN_DISPLACEMENT = 0.06
MIN_TRACKS_PER_CAMERA = 25
# Circular concentration above which a camera counts as unidirectional and a
# permitted heading is a meaningful claim. 0.55 separates a one-way approach
# from a junction on this estate; below it no direction zone is created.
MIN_HEADING_CONCENTRATION = 0.55
# Fraction of vehicle path points the carriageway polygon must cover. Lower is
# tighter — the busy lanes rather than every place a vehicle was ever seen.
CARRIAGEWAY_DENSITY = 0.55
# Two flows must be at least this far apart to count as opposing rather than one
# stream curving through the frame.
MIN_OPPOSING_DEG = 130
# Per-lane polygons are tighter than the carriageway: a lane is the ground its
# own direction of traffic actually uses.
LANE_DENSITY = 0.45
# Above this overlap the two "lanes" are the same tarmac, which means a junction
# rather than a divided road — and no permitted direction can be claimed.
MAX_LANE_OVERLAP = 0.35
# Points a person must spend inside the carriageway before it is an intrusion
# rather than a crossing. See the rationale on the zone params below.
INTRUSION_MIN_POINTS = 12
# The hull is grown slightly: vehicles do use the full width of a carriageway,
# and a hull drawn tight around observed centres clips the lanes at its edges.
HULL_DILATE = 0.03


def circular_mean_deg(degs: list[float]) -> float:
    if not degs:
        return 0.0
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(y, x)) % 360.0


def hull_polygon(
    points: list[tuple[float, float]], dilate: float = HULL_DILATE
) -> list[list[float]]:
    """Convex hull of normalised points, grown by *dilate* about its centroid."""
    if len(points) < 3:
        return []
    arr = np.array(points, dtype=np.float32)
    hull = cv2.convexHull(arr)
    poly = [[float(p[0][0]), float(p[0][1])] for p in hull]
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)
    grown = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        n = math.hypot(dx, dy) or 1.0
        grown.append([
            max(0.0, min(1.0, x + dilate * dx / n)),
            max(0.0, min(1.0, y + dilate * dy / n)),
        ])
    return grown


def heading_concentration(degs: list[float]) -> float:
    """Circular concentration R of a set of headings, 0–1.

    R is the length of the mean resultant vector: 1.0 when every track runs the
    same way, near 0 when they are spread around the compass. It is the test
    that decides whether "the permitted direction here" is a meaningful idea at
    all — on a one-way approach it is, at a junction it is not.
    """
    if not degs:
        return 0.0
    x = sum(math.cos(math.radians(d)) for d in degs) / len(degs)
    y = sum(math.sin(math.radians(d)) for d in degs) / len(degs)
    return math.hypot(x, y)


def dense_region(points: list[tuple[float, float]], keep: float = 0.6,
                 grid: int = 24) -> list[list[float]]:
    """Hull of the cells holding the densest *keep* fraction of points.

    The convex hull of every vehicle path is the wrong shape for a restricted
    zone: it stretches to the furthest stray track and swallows the footpaths,
    so every pedestrian near the road reads as an intrusion. Binning to a grid
    and keeping only the busy cells gives the *live* carriageway — the ground a
    person should genuinely not be standing on.
    """
    if len(points) < 12:
        return []
    counts: dict[tuple[int, int], int] = {}
    for x, y in points:
        gx, gy = min(grid - 1, int(x * grid)), min(grid - 1, int(y * grid))
        counts[(gx, gy)] = counts.get((gx, gy), 0) + 1
    if not counts:
        return []
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    total = sum(counts.values())
    running, cells = 0, []
    for cell, n in ordered:
        cells.append(cell)
        running += n
        if running >= total * keep:
            break
    corners: list[tuple[float, float]] = []
    for gx, gy in cells:
        corners.extend([
            (gx / grid, gy / grid), ((gx + 1) / grid, gy / grid),
            (gx / grid, (gy + 1) / grid), ((gx + 1) / grid, (gy + 1) / grid),
        ])
    return hull_polygon(corners, dilate=0.0)


def poly_area(poly: list[list[float]]) -> float:
    if len(poly) < 3:
        return 0.0
    return float(cv2.contourArea(np.array(poly, dtype=np.float32)))


def poly_overlap(a: list[list[float]], b: list[list[float]]) -> float:
    """Intersection area as a fraction of the smaller polygon, 0–1."""
    if len(a) < 3 or len(b) < 3:
        return 1.0
    pa = np.array(a, dtype=np.float32)
    pb = np.array(b, dtype=np.float32)
    inter, _ = cv2.intersectConvexConvex(pa, pb)
    smaller = min(poly_area(a), poly_area(b))
    return float(inter) / smaller if smaller > 0 else 1.0


def separated_lanes(tracks: list[dict]) -> list[tuple[float, list[list[float]], int]]:
    """Opposing flows that occupy *different ground*, as (heading, polygon, n).

    This is what makes wrong-way detection possible on a two-way road, and what
    the first attempt got wrong. Clustering by heading alone produced polygons
    that all covered the same tarmac, so a vehicle driving correctly on one
    carriageway sat inside the opposing carriageway's zone and was judged
    against a heading that never applied to it — 180 false wrong-way events.

    A real two-way road separates its directions in space: northbound traffic is
    on one side of the centre line and southbound on the other. So the test is
    not "are there two headings" but "do the two headings occupy distinct
    ground". Where they do, each side gets its own zone and a vehicle against
    the flow *on that side* is genuinely wrong-way. Where they do not — a
    junction, a roundabout, an open forecourt — no direction zone is created,
    because there is no permitted direction to be against.

    Returns an empty list unless exactly that two-lane structure is present.
    """
    bins: dict[int, list[dict]] = {}
    for t in tracks:
        b = int(float(t["heading_deg"]) // HEADING_BIN_DEG) % (360 // HEADING_BIN_DEG)
        bins.setdefault(b, []).append(t)

    flows = [(b, ts) for b, ts in bins.items() if len(ts) >= MIN_TRACKS_PER_FLOW]
    if len(flows) < 2:
        return []
    flows.sort(key=lambda bt: -len(bt[1]))

    (_, ts_a), (_, ts_b) = flows[0], flows[1]
    head_a = circular_mean_deg([float(t["heading_deg"]) for t in ts_a])
    head_b = circular_mean_deg([float(t["heading_deg"]) for t in ts_b])

    # Opposing, not merely different: two approaches 40° apart are the same
    # stream of traffic curving, and separating them buys nothing.
    delta = abs((head_a - head_b + 180) % 360 - 180)
    if delta < MIN_OPPOSING_DEG:
        return []

    pts_a: list[tuple[float, float]] = []
    pts_b: list[tuple[float, float]] = []
    for t in ts_a:
        pts_a.extend(ground_points(t["path"]))
    for t in ts_b:
        pts_b.extend(ground_points(t["path"]))

    poly_a = dense_region(pts_a, keep=LANE_DENSITY)
    poly_b = dense_region(pts_b, keep=LANE_DENSITY)
    if len(poly_a) < 3 or len(poly_b) < 3:
        return []
    if poly_overlap(poly_a, poly_b) > MAX_LANE_OVERLAP:
        return []

    return [(head_a, poly_a, len(ts_a)), (head_b, poly_b, len(ts_b))]


def ground_points(path_raw) -> list[tuple[float, float]]:
    """Where each path point touched the road, normalised.

    ``path`` stores the point the analytics layer already computed as the
    track's ground contact, so this is a read rather than a projection.
    """
    if isinstance(path_raw, str):
        path_raw = json.loads(path_raw)
    out = []
    for p in path_raw or []:
        if p.get("x") is not None and p.get("y") is not None:
            out.append((float(p["x"]), float(p["y"])))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="delete zones previously created by this script first")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    write = conn.cursor()

    if args.replace and not args.dry_run:
        write.execute("DELETE FROM zones WHERE created_by = 'derived'")
        log.info("Removed %d previously derived zones", write.rowcount)

    cur.execute(
        """SELECT t.camera_id, c.external_id, c.name, t.class, t.heading_deg,
                  t.displacement_norm, t.path
           FROM tracks t JOIN cameras c ON c.id = t.camera_id
           WHERE t.class = ANY(%s) AND t.heading_deg IS NOT NULL
             AND t.displacement_norm >= %s""",
        (list(VEHICLES), MIN_DISPLACEMENT),
    )
    rows = cur.fetchall()
    log.info("%d moving vehicle tracks with a heading", len(rows))

    by_cam: dict[str, list[dict]] = {}
    for r in rows:
        by_cam.setdefault(str(r["camera_id"]), []).append(r)

    created = {"direction": 0, "restricted": 0}
    print(f"{'camera':<9}{'tracks':>7}{'flows':>7}  {'headings':<26}{'name'}")
    print("-" * 86)

    for cam_id, tracks in sorted(by_cam.items(), key=lambda kv: -len(kv[1])):
        if len(tracks) < MIN_TRACKS_PER_CAMERA:
            continue
        ext = tracks[0]["external_id"]
        cam_name = tracks[0]["name"] or ext

        # ── Cluster by heading bin ───────────────────────────────────────────
        bins: dict[int, list[dict]] = {}
        for t in tracks:
            b = int(float(t["heading_deg"]) // HEADING_BIN_DEG) % (360 // HEADING_BIN_DEG)
            bins.setdefault(b, []).append(t)

        flows = [(b, ts) for b, ts in bins.items() if len(ts) >= MIN_TRACKS_PER_FLOW]
        flows.sort(key=lambda bt: -len(bt[1]))

        # ── Is "the permitted direction" even meaningful here? ───────────────
        # Creating one direction zone per flow was wrong, and measurably so: the
        # flow hulls overlap at a junction, so a vehicle correctly following one
        # approach sits inside another approach's polygon and is judged against
        # a heading that never applied to it. That produced 180 wrong-way events
        # from 509 moving tracks — 35% of traffic, which is not a detection rate,
        # it is a broken premise.
        #
        # The honest test is whether this camera's traffic is unidirectional at
        # all. On a one-way approach the headings concentrate and a permitted
        # direction exists; at a junction they do not, and no polygon recovers
        # it. Cameras that fail this get no direction zone, and wrong-way is
        # simply not claimed for them.
        all_headings = [float(t["heading_deg"]) for t in tracks]
        R = heading_concentration(all_headings)
        headings_desc = []

        if R >= MIN_HEADING_CONCENTRATION:
            # One-way: every vehicle runs the same way, so the whole carriageway
            # carries one permitted heading.
            pts: list[tuple[float, float]] = []
            for t in tracks:
                pts.extend(ground_points(t["path"]))
            lanes = [(circular_mean_deg(all_headings), hull_polygon(pts), len(tracks))]
            headings_desc.append(f"one-way {lanes[0][0]:.0f}° R={R:.2f}")
        else:
            # Otherwise the only recoverable structure is a two-way road whose
            # directions sit on different ground.
            lanes = separated_lanes(tracks)
            if lanes:
                headings_desc.append(
                    "two-way " + "/".join(f"{h:.0f}°" for h, _, _ in lanes))
            else:
                headings_desc.append(f"no permitted dir R={R:.2f}")

        for heading, poly, n in lanes:
            if len(poly) < 3:
                continue
            if not args.dry_run:
                write.execute(
                    """INSERT INTO zones (camera_id, name, kind, polygon, direction_deg,
                                          tolerance_deg, classes, created_by, params)
                       VALUES (%s,%s,'direction',%s::jsonb,%s,%s,%s,'derived',%s::jsonb)""",
                    (cam_id,
                     f"{cam_name} — permitted flow {heading:.0f}°",
                     json.dumps(poly), heading, 75.0, list(VEHICLES),
                     json.dumps({"derived_from_tracks": n,
                                 "heading_concentration": round(R, 3)})),
                )
            created["direction"] += 1

        # ── The live carriageway, watched for pedestrians ───────────────────
        # Density-based rather than the full hull, for the same reason: the hull
        # of every vehicle path reaches the footpath and flags every pedestrian
        # walking beside the road. The busy cells are the lanes themselves.
        all_pts: list[tuple[float, float]] = []
        for t in tracks:
            all_pts.extend(ground_points(t["path"]))
        carriageway = dense_region(all_pts, keep=CARRIAGEWAY_DENSITY)
        if len(carriageway) >= 3:
            if not args.dry_run:
                write.execute(
                    """INSERT INTO zones (camera_id, name, kind, polygon,
                                          tolerance_deg, classes, created_by, params)
                       VALUES (%s,%s,'restricted',%s::jsonb,%s,%s,'derived',%s::jsonb)""",
                    (cam_id,
                     f"{cam_name} — live carriageway",
                     json.dumps(carriageway), 75.0, ["person"],
                     json.dumps({"derived_from_tracks": len(tracks),
                                 "density_fraction": CARRIAGEWAY_DENSITY,
                                 "rationale": "a person standing in live traffic",
                                 # The default of 3 points inside fires on anyone
                                 # who crosses the road, and in Gujarat traffic
                                 # that is 223 events from 1,665 tracks — normal
                                 # behaviour, reported as an anomaly. Crossing is
                                 # not the hazard; *staying* is. Person tracks on
                                 # this estate run 1-5 points for a crossing and
                                 # past 15 only when someone stops, so the gate
                                 # sits between the two.
                                 "intrusion_min_points_inside":
                                     INTRUSION_MIN_POINTS})),
                )
            created["restricted"] += 1

        print(f"{ext:<9}{len(tracks):>7}{len(flows):>7}  "
              f"{', '.join(headings_desc)[:24]:<26}{cam_name[:32]}")

    if not args.dry_run:
        conn.commit()

    print("-" * 86)
    print(f"  direction zones   {created['direction']}")
    print(f"  restricted zones  {created['restricted']}")
    print("\n  " + ("Dry run — nothing written" if args.dry_run
                    else "Written. Re-run: python -m ml.analytics.runner -v"))


if __name__ == "__main__":
    main()
