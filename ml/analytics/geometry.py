"""Pure-geometry helpers for the rule engine. No numpy, no cv2 — these run in
the API process too, where importing the CV stack would cost 400 MB for nothing.

Coordinate conventions
----------------------
* ``bbox`` is the dict the pipeline writes: ``{"x1","y1","x2","y2"}`` in pixels.
* Zone polygons are **normalised** ``[[x, y], …]`` in 0–1, so a zone drawn on a
  1080p feed still means the same thing if the feed switches to 720p.
* A track's ground-contact point is the *bottom centre* of its box, not the
  centroid. For anything spatial (is it inside the zone, which way is it going)
  bottom-centre is the right anchor: it is where the object touches the road,
  and it does not drift as the box grows on approach.
"""

from __future__ import annotations

import math

Point = tuple[float, float]


def bbox_centroid(bbox: dict) -> Point:
    """Geometric centre of a bbox, in pixels."""
    return ((bbox["x1"] + bbox["x2"]) / 2.0, (bbox["y1"] + bbox["y2"]) / 2.0)


def bbox_ground_point(bbox: dict) -> Point:
    """Bottom-centre of a bbox — where the object meets the ground plane."""
    return ((bbox["x1"] + bbox["x2"]) / 2.0, float(bbox["y2"]))


def normalise(pt: Point, width: float, height: float) -> Point:
    """Pixel point → 0–1 normalised, clamped."""
    if width <= 0 or height <= 0:
        return (0.0, 0.0)
    return (min(1.0, max(0.0, pt[0] / width)), min(1.0, max(0.0, pt[1] / height)))


def point_in_polygon(pt: Point, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test.

    Handles concave polygons and self-touching edges. Points exactly on an edge
    are treated as inside on the left/top edges and outside on right/bottom,
    which is the standard half-open convention — it means adjacent zones tile
    without double-counting a straddling object.
    """
    if not polygon or len(polygon) < 3:
        return False
    x, y = pt
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y):
            # x coordinate where edge (j→i) crosses the horizontal line y
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_area(polygon: list[list[float]]) -> float:
    """Shoelace area. Sign-agnostic, so winding order does not matter."""
    if not polygon or len(polygon) < 3:
        return 0.0
    total = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i][0], polygon[i][1]
        x2, y2 = polygon[(i + 1) % n][0], polygon[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def heading_deg(a: Point, b: Point) -> float:
    """Compass-style heading from *a* to *b* in image space, degrees 0–360.

    0° = up the frame (decreasing y, i.e. "away from camera" on most road
    views), 90° = right, 180° = down, 270° = left. Image y grows downward, so
    the y term is negated to make this read like a compass.
    """
    dx = b[0] - a[0]
    dy = -(b[1] - a[1])
    if dx == 0 and dy == 0:
        return 0.0
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def angle_delta(a_deg: float, b_deg: float) -> float:
    """Smallest absolute angle between two headings, 0–180."""
    d = abs((a_deg - b_deg) % 360.0)
    return d if d <= 180.0 else 360.0 - d


def bbox_iou(a: dict, b: dict) -> float:
    """Intersection-over-union of two bboxes."""
    ix1, iy1 = max(a["x1"], b["x1"]), max(a["y1"], b["y1"])
    ix2, iy2 = min(a["x2"], b["x2"]), min(a["y2"], b["y2"])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a["x2"] - a["x1"]) * max(0.0, a["y2"] - a["y1"])
    area_b = max(0.0, b["x2"] - b["x1"]) * max(0.0, b["y2"] - b["y1"])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def bbox_diag(bbox: dict) -> float:
    """Diagonal length of a bbox — the natural scale for "did it move much"."""
    w = max(0.0, bbox["x2"] - bbox["x1"])
    h = max(0.0, bbox["y2"] - bbox["y1"])
    return math.hypot(w, h)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres.

    Used only for *bootstrapping* the camera graph before any co-observation
    exists. Model 1's handover warns that most camera coordinates carry
    ``geocode_precision: "district"`` — placed from the camera's name to a
    district representative point — so a distance between two of those is not a
    real distance. Callers must treat this as a weak prior and let observed
    transit times override it.
    """
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))
