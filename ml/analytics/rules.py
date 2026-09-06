"""Rule-based event detection over folded tracks — features 15–20.

Six detectors, plus a track-level stopped-vehicle rule that works on archive
footage where ``ml/worker.py``'s live detector is switched off:

    loitering          dwelling in place for too long
    wrong_way          travelling against a zone's permitted direction
    crowd              too many people on one camera, sustained
    zone_intrusion     entering a restricted polygon
    abandoned_object   a static object left behind with nobody near it
    accident_candidate two vehicles overlapping, then both stopping
    stopped_vehicle    a vehicle stationary far longer than traffic explains

Design commitments
------------------
**Tracks, not frames.** Every rule reads ``tracks``. A frame cannot tell you
something dwelled, and a frame-level rule fires once per frame — thirty alerts
for one loiterer. Folding first is what makes these rules cheap and quiet.

**Idempotent by construction.** Each event carries a deterministic
``dedupe_key``, and inserts are ``ON CONFLICT (dedupe_key) DO NOTHING``. Re-run
the engine as often as you like: no duplicates, and — importantly — an event an
operator already marked ``false_positive`` is never resurrected or silently
reset. Triage state belongs to the operator, not the rule.

**Honest confidence.** These are heuristics over bounding boxes from a
single uncalibrated monocular camera. None of them is a trained classifier, and
`accident_candidate` in particular is named "candidate" because that is what it
produces. Every event records the numbers it fired on in ``metrics`` so an
operator can see the reasoning, disagree with it, and have that disagreement
stick.

**No CV stack.** Pure Python and SQL, so the engine runs in the API process, on
a laptop, or on a cron box with no GPU.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from ml.analytics.geometry import (
    angle_delta,
    bbox_iou,
    heading_deg,
    point_in_polygon,
)

log = logging.getLogger(__name__)

# Classes the pipeline can currently emit (CLIP zero-shot label set in
# ml/pipeline.py). Kept as sets rather than hardcoded strings at each call site
# so adding a class to the pipeline needs one edit here, not seven.
PERSON_CLASSES = {"person"}
VEHICLE_CLASSES = {"car", "motorcycle", "bus", "truck", "auto_rickshaw", "tractor"}
# Static-object classes for abandoned-object detection. The pipeline does not
# emit these yet — it filters YOLO to VEHICLE_CLASSES and relabels with CLIP.
# The rule is written and tested against them so that enabling the wider class
# set is a pipeline change only; until then this detector legitimately returns
# zero events, and says so rather than pretending to work.
OBJECT_CLASSES = {"backpack", "handbag", "suitcase", "bag", "box", "package"}


@dataclass
class RuleParams:
    """Thresholds for the rule engine.

    Defaults are tuned for the Sentinel sandbox footage: ~6 processed FPS,
    fixed traffic-junction cameras, 1080p-or-smaller feeds. They are
    deliberately conservative — a control room that stops trusting its alerts
    because half of them are noise is worse off than one with no alerts.

    Per-zone overrides live in ``zones.params`` and are merged over these, so a
    pedestrian underpass can use a 300-second loitering threshold while a bank
    forecourt uses 45, without a redeploy.
    """

    # ── loitering ────────────────────────────────────────────────────────────
    loiter_min_dwell_s: float = 120.0
    """A person present this long, without going anywhere, is loitering."""
    loiter_max_spread_norm: float = 0.10
    """…where "without going anywhere" means their path stayed inside a circle
    of this radius (fraction of frame diagonal). Spread, not displacement:
    someone pacing a 10-metre stretch for ten minutes has near-zero net
    displacement but is not standing still, and both are loitering."""
    loiter_min_frames: int = 12

    # ── stopped vehicle ──────────────────────────────────────────────────────
    stopped_min_dwell_s: float = 180.0
    """Longer than a long red light. ml/worker.py's live detector uses a 20px /
    30-frame rule; at track level we can afford to wait for real evidence."""
    stopped_max_spread_norm: float = 0.04
    stopped_min_frames: int = 20

    # ── wrong way ────────────────────────────────────────────────────────────
    wrongway_min_displacement_norm: float = 0.18
    """Don't judge direction from a track that barely moved — heading over a
    short path is tracker jitter, and a stationary vehicle has no direction."""
    wrongway_min_frames: int = 8
    wrongway_default_tolerance_deg: float = 75.0

    # ── crowd ────────────────────────────────────────────────────────────────
    crowd_min_people: int = 12
    crowd_bucket_s: int = 30
    """Count distinct person tracks present per bucket, not detections. A
    detection count of 200 might be 200 people or one person for 200 frames."""
    crowd_sustained_buckets: int = 2
    """Must hold across consecutive buckets — a bus unloading for one 30-second
    window is not a crowd event worth waking anyone for."""

    # ── zone intrusion ───────────────────────────────────────────────────────
    intrusion_min_points_inside: int = 3
    """Require several path points inside the polygon. One point inside is a
    box-jitter artefact on the boundary, which is where most tracks pass."""

    # ── abandoned object ─────────────────────────────────────────────────────
    abandoned_min_dwell_s: float = 300.0
    abandoned_max_spread_norm: float = 0.03
    abandoned_person_clearance_norm: float = 0.12
    """No person track came within this distance during the dwell. An object
    with its owner standing next to it is luggage, not a threat."""

    # ── accident candidate ───────────────────────────────────────────────────
    accident_min_iou: float = 0.15
    """Box overlap between two vehicle tracks. Low on purpose: from a fixed
    camera at a shallow angle, two vehicles that have genuinely collided often
    overlap far less in image space than intuition suggests, while two in
    adjacent lanes can overlap more."""
    accident_max_speed_after: float = 0.004
    """Both tracks must go near-stationary after contact: normalised distance
    per second, so ~0.4% of the frame per second."""
    accident_min_speed_before: float = 0.02
    """…and at least one must have been moving properly beforehand. Two parked
    cars whose boxes overlap are not an accident."""
    accident_min_stationary_s: float = 8.0
    accident_time_tolerance_s: float = 1.5
    """How far apart two path samples may be and still count as simultaneous."""

    def merged(self, overrides: Optional[dict]) -> "RuleParams":
        """Return a copy with ``overrides`` applied. Unknown keys are ignored —
        a zone carrying a param this version doesn't know about must not crash
        the engine."""
        if not overrides:
            return self
        base = asdict(self)
        for k, v in overrides.items():
            if k in base and v is not None:
                base[k] = v
        return RuleParams(**base)


# ── what a rule produces ─────────────────────────────────────────────────────

@dataclass
class EventDraft:
    """One candidate event. Rules return these; nothing is written until
    :func:`write_events` is called, which keeps every detector a pure function
    and therefore testable without a database — worth having, given the rules
    are the part most likely to need tuning against real footage."""

    camera_id: str
    event_type: str
    started_at: datetime
    severity: str = "medium"
    label: str = ""
    track_uuid: Optional[str] = None
    detection_id: Optional[str] = None
    zone_id: Optional[str] = None
    ended_at: Optional[datetime] = None
    pts_ms: Optional[float] = None
    confidence: float = 0.5
    metrics: dict = field(default_factory=dict)
    thumbnail_key: Optional[str] = None
    plate_text: Optional[str] = None
    dedupe_key: str = ""
    source: str = "analytics"

    def with_key(self, *parts: Any) -> "EventDraft":
        """Attach a deterministic dedupe key built from stable identifiers.

        Stable is the whole point: the key must not contain anything that shifts
        between runs (a wall-clock timestamp, a random id, a float that rounds
        differently) or idempotency is lost and every run re-alerts.
        """
        self.dedupe_key = ":".join([self.event_type] + [str(p) for p in parts])
        return self


_INSERT_EVENT = """
INSERT INTO events (
    camera_id, event_type, severity, status, label, track_uuid, detection_id,
    zone_id, started_at, ended_at, pts_ms, confidence, metrics, thumbnail_key,
    plate_text, dedupe_key, source
) VALUES (
    %s, %s, %s, 'new', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
)
ON CONFLICT (dedupe_key) DO NOTHING
RETURNING id
"""


def write_events(conn, drafts: Iterable[EventDraft]) -> tuple[int, int]:
    """Persist drafts. Returns ``(inserted, suppressed_as_duplicate)``.

    ``DO NOTHING`` rather than ``DO UPDATE`` is deliberate. An operator may have
    already acknowledged, tagged, or dismissed this event; overwriting it with
    freshly computed metrics would reset that judgement and, worse, would make a
    dismissed false positive reappear as new on every cron tick.
    """
    inserted = duplicate = 0
    with conn.cursor() as cur:
        for d in drafts:
            if not d.dedupe_key:
                raise ValueError(f"event draft without dedupe_key: {d.event_type}")
            cur.execute(_INSERT_EVENT, (
                d.camera_id, d.event_type, d.severity, d.label, d.track_uuid,
                d.detection_id, d.zone_id, d.started_at, d.ended_at, d.pts_ms,
                round(float(d.confidence), 4), json.dumps(d.metrics, default=str),
                d.thumbnail_key, d.plate_text, d.dedupe_key, d.source,
            ))
            if cur.fetchone():
                inserted += 1
            else:
                duplicate += 1
    return inserted, duplicate


# ── path helpers ─────────────────────────────────────────────────────────────

def _path(track: dict) -> list[dict]:
    raw = track.get("path") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [p for p in raw if p.get("x") is not None and p.get("y") is not None]


def _clock(point: dict) -> Optional[float]:
    """Seconds-since-start for a path point.

    Prefers ``pts_ms`` — position in the source video — over ``t``, which is
    wall-clock at inference time and is meaningless for archive footage
    processed faster than real time.
    """
    if point.get("pts_ms") is not None:
        return float(point["pts_ms"]) / 1000.0
    t = point.get("t")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).timestamp()
    except (TypeError, ValueError):
        return None


def _box(point: dict) -> dict:
    """Reconstruct a normalised bbox from a path point.

    Path points store the *ground* point (bottom-centre) plus normalised width
    and height, so the box is recovered by growing upward and outward from it.
    """
    w = float(point.get("w") or 0.0)
    h = float(point.get("h") or 0.0)
    x, y = float(point["x"]), float(point["y"])
    return {"x1": x - w / 2, "y1": y - h, "x2": x + w / 2, "y2": y}


def _mean_speed(points: list[dict]) -> float:
    """Mean normalised distance travelled per second over a run of points."""
    if len(points) < 2:
        return 0.0
    dist = 0.0
    for a, b in zip(points, points[1:]):
        dist += math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    t0, t1 = _clock(points[0]), _clock(points[-1])
    span = (t1 - t0) if (t0 is not None and t1 is not None) else 0.0
    return dist / span if span > 0.05 else 0.0


# ── 15. loitering ────────────────────────────────────────────────────────────

def detect_loitering(tracks: list[dict], p: RuleParams) -> list[EventDraft]:
    """A person present for a long time who never went anywhere.

    Two conditions, both necessary. Dwell time alone flags anyone caught in
    traffic. Small *spread* alone flags anyone standing at a crossing for
    fifteen seconds. Together they describe someone who chose to stay.
    """
    out: list[EventDraft] = []
    for t in tracks:
        if t["class"] not in PERSON_CLASSES:
            continue
        if t["detection_count"] < p.loiter_min_frames:
            continue
        dwell = float(t["duration_s"] or 0.0)
        spread = float(t["max_spread_norm"] or 0.0)
        if dwell < p.loiter_min_dwell_s or spread > p.loiter_max_spread_norm:
            continue

        # Confidence scales with how far past the threshold it is, capped: a
        # 40-minute dwell is not 20× more certain than a 2-minute one, it is
        # just further past a line we drew.
        over = dwell / p.loiter_min_dwell_s
        conf = min(0.92, 0.55 + 0.12 * math.log(over) / math.log(2)) if over >= 1 else 0.5
        out.append(EventDraft(
            camera_id=str(t["camera_id"]),
            event_type="loitering",
            severity="medium" if dwell < 4 * p.loiter_min_dwell_s else "high",
            label=f"Person loitering {int(dwell)}s",
            track_uuid=str(t["id"]),
            detection_id=str(t["best_detection_id"]) if t.get("best_detection_id") else None,
            started_at=t["first_seen"],
            ended_at=t["last_seen"],
            pts_ms=t.get("first_pts_ms"),
            confidence=conf,
            thumbnail_key=t.get("thumbnail_key"),
            metrics={
                "dwell_s": round(dwell, 1),
                "max_spread_norm": round(spread, 4),
                "displacement_norm": round(float(t.get("displacement_norm") or 0.0), 4),
                "frames": t["detection_count"],
                "threshold_dwell_s": p.loiter_min_dwell_s,
                "threshold_spread_norm": p.loiter_max_spread_norm,
            },
        ).with_key(t["id"]))
    return out


# ── stopped vehicle (track-level; live detector is frame-level) ───────────────

def detect_stopped_vehicles(tracks: list[dict], p: RuleParams) -> list[EventDraft]:
    """A vehicle stationary for longer than traffic explains.

    ``ml/worker.py`` has a live frame-level version of this (20 px of centroid
    movement over 30 frames). This one exists because batch indexing runs with
    anomaly detection switched off, so an archive pass would otherwise find
    nothing — and because at track level the evidence is stronger: real duration
    from ``pts_ms``, and path spread rather than centroid delta between two
    arbitrary frames.
    """
    out: list[EventDraft] = []
    for t in tracks:
        if t["class"] not in VEHICLE_CLASSES:
            continue
        if t["detection_count"] < p.stopped_min_frames:
            continue
        dwell = float(t["duration_s"] or 0.0)
        spread = float(t["max_spread_norm"] or 0.0)
        if dwell < p.stopped_min_dwell_s or spread > p.stopped_max_spread_norm:
            continue
        out.append(EventDraft(
            camera_id=str(t["camera_id"]),
            event_type="stopped_vehicle",
            severity="medium" if dwell < 600 else "high",
            label=f"{t['class'].replace('_', ' ').title()} stopped {int(dwell)}s",
            track_uuid=str(t["id"]),
            detection_id=str(t["best_detection_id"]) if t.get("best_detection_id") else None,
            started_at=t["first_seen"],
            ended_at=t["last_seen"],
            pts_ms=t.get("first_pts_ms"),
            confidence=min(0.9, 0.6 + 0.1 * (dwell / p.stopped_min_dwell_s - 1)),
            thumbnail_key=t.get("thumbnail_key"),
            plate_text=t.get("plate_text"),
            metrics={
                "dwell_s": round(dwell, 1),
                "max_spread_norm": round(spread, 4),
                "frames": t["detection_count"],
                "class": t["class"],
            },
        ).with_key(t["id"]))
    return out


# ── 16. wrong way ────────────────────────────────────────────────────────────

def detect_wrong_way(
    tracks: list[dict], zones: list[dict], p: RuleParams
) -> list[EventDraft]:
    """Travelling against a direction zone's permitted heading.

    Why a zone and not a global per-camera direction: a junction has several
    approaches, each with its own legal direction, and one camera usually sees
    more than one of them. A single "traffic flows north here" setting would
    flag every correct turn.

    Only tracks that actually crossed the zone are judged, and only tracks that
    moved far enough for their heading to mean anything.
    """
    dir_zones = [z for z in zones if z["kind"] == "direction" and z.get("direction_deg") is not None]
    if not dir_zones:
        return []
    by_camera: dict[str, list[dict]] = {}
    for z in dir_zones:
        by_camera.setdefault(str(z["camera_id"]), []).append(z)

    out: list[EventDraft] = []
    for t in tracks:
        zs = by_camera.get(str(t["camera_id"]))
        if not zs:
            continue
        if t["detection_count"] < p.wrongway_min_frames:
            continue
        if float(t.get("displacement_norm") or 0.0) < p.wrongway_min_displacement_norm:
            continue
        heading = t.get("heading_deg")
        if heading is None:
            continue

        pts = _path(t)
        for z in zs:
            zp = z.get("params") or {}
            zparams = p.merged(zp if isinstance(zp, dict) else json.loads(zp))
            classes = z.get("classes") or []
            if classes and t["class"] not in classes:
                continue
            polygon = z["polygon"] if isinstance(z["polygon"], list) else json.loads(z["polygon"])
            inside = [q for q in pts if point_in_polygon((q["x"], q["y"]), polygon)]
            if len(inside) < max(2, zparams.intrusion_min_points_inside):
                continue

            # Heading measured across the in-zone segment only, not the whole
            # track: a vehicle that enters the frame from the side, turns into
            # the lane and then drives the wrong way has a net heading that
            # averages the turn away.
            seg_heading = heading_deg((inside[0]["x"], inside[0]["y"]),
                                      (inside[-1]["x"], inside[-1]["y"]))
            tol = float(z.get("tolerance_deg") or zparams.wrongway_default_tolerance_deg)
            delta = angle_delta(seg_heading, float(z["direction_deg"]))
            if delta <= tol:
                continue

            # 180° against the flow is unambiguous; just past tolerance is not.
            conf = min(0.95, 0.45 + 0.5 * (delta - tol) / max(1.0, 180.0 - tol))
            out.append(EventDraft(
                camera_id=str(t["camera_id"]),
                event_type="wrong_way",
                severity="high" if delta > 135 else "medium",
                label=f"Wrong way in {z['name']} ({int(delta)}° off)",
                track_uuid=str(t["id"]),
                detection_id=str(t["best_detection_id"]) if t.get("best_detection_id") else None,
                zone_id=str(z["id"]),
                started_at=t["first_seen"],
                ended_at=t["last_seen"],
                pts_ms=t.get("first_pts_ms"),
                confidence=conf,
                thumbnail_key=t.get("thumbnail_key"),
                plate_text=t.get("plate_text"),
                metrics={
                    "segment_heading_deg": round(seg_heading, 1),
                    "track_heading_deg": round(float(heading), 1),
                    "zone_direction_deg": float(z["direction_deg"]),
                    "angle_delta_deg": round(delta, 1),
                    "tolerance_deg": tol,
                    "points_in_zone": len(inside),
                    "displacement_norm": round(float(t.get("displacement_norm") or 0.0), 4),
                },
            ).with_key(t["id"], z["id"]))
    return out


# ── 19. restricted-zone intrusion ────────────────────────────────────────────

def detect_zone_intrusion(
    tracks: list[dict], zones: list[dict], p: RuleParams
) -> list[EventDraft]:
    """A track whose ground point entered a restricted polygon.

    The *ground* point, not the centroid: a tall object standing outside a fence
    has a centroid that projects inside it. Where the object touches the road is
    the only defensible answer to "was it in the zone".
    """
    r_zones = [z for z in zones if z["kind"] == "restricted"]
    if not r_zones:
        return []
    by_camera: dict[str, list[dict]] = {}
    for z in r_zones:
        by_camera.setdefault(str(z["camera_id"]), []).append(z)

    out: list[EventDraft] = []
    for t in tracks:
        zs = by_camera.get(str(t["camera_id"]))
        if not zs:
            continue
        pts = _path(t)
        if not pts:
            continue
        for z in zs:
            classes = z.get("classes") or []
            if classes and t["class"] not in classes:
                continue
            zp = z.get("params") or {}
            zparams = p.merged(zp if isinstance(zp, dict) else json.loads(zp))
            polygon = z["polygon"] if isinstance(z["polygon"], list) else json.loads(z["polygon"])
            inside = [q for q in pts if point_in_polygon((q["x"], q["y"]), polygon)]
            if len(inside) < zparams.intrusion_min_points_inside:
                continue

            t0, t1 = _clock(inside[0]), _clock(inside[-1])
            dwell = (t1 - t0) if (t0 is not None and t1 is not None) else 0.0
            fraction = len(inside) / len(pts)
            # A track that spent its whole life inside the zone is a stronger
            # signal than one that clipped a corner in passing.
            conf = min(0.95, 0.5 + 0.45 * fraction)
            out.append(EventDraft(
                camera_id=str(t["camera_id"]),
                event_type="zone_intrusion",
                severity="high" if t["class"] in PERSON_CLASSES else "medium",
                label=f"{t['class'].replace('_', ' ').title()} in {z['name']}",
                track_uuid=str(t["id"]),
                detection_id=inside[0].get("d") or (
                    str(t["best_detection_id"]) if t.get("best_detection_id") else None),
                zone_id=str(z["id"]),
                started_at=t["first_seen"],
                ended_at=t["last_seen"],
                pts_ms=inside[0].get("pts_ms") or t.get("first_pts_ms"),
                confidence=conf,
                thumbnail_key=t.get("thumbnail_key"),
                plate_text=t.get("plate_text"),
                metrics={
                    "points_in_zone": len(inside),
                    "path_points": len(pts),
                    "fraction_inside": round(fraction, 3),
                    "dwell_in_zone_s": round(max(0.0, dwell), 1),
                    "class": t["class"],
                },
            ).with_key(t["id"], z["id"]))
    return out


# ── 18. crowd ────────────────────────────────────────────────────────────────

def detect_crowd(tracks: list[dict], p: RuleParams) -> list[EventDraft]:
    """Too many distinct people on one camera, sustained across buckets.

    Counting *tracks* rather than detections is the whole trick. A naive
    person-detection count fires on one person standing in view for a minute
    (360 detections at 6 FPS) and misses twenty people walking through quickly.
    Bucketing by time and requiring the count to hold across consecutive buckets
    filters the bus-unloading case, which is a crowd for fifteen seconds and not
    a situation.
    """
    # camera → bucket index → set of track ids present
    buckets: dict[str, dict[int, set]] = {}
    bucket_span: dict[str, dict[int, tuple[float, float]]] = {}
    origin: dict[str, float] = {}

    persons = [t for t in tracks if t["class"] in PERSON_CLASSES]
    for t in persons:
        cam = str(t["camera_id"])
        pts = _path(t)
        for q in pts:
            clock = _clock(q)
            if clock is None:
                continue
            origin.setdefault(cam, clock)
            idx = int((clock - origin[cam]) // p.crowd_bucket_s)
            buckets.setdefault(cam, {}).setdefault(idx, set()).add(str(t["id"]))
            lo, hi = bucket_span.setdefault(cam, {}).get(idx, (clock, clock))
            bucket_span[cam][idx] = (min(lo, clock), max(hi, clock))

    out: list[EventDraft] = []
    for cam, per_bucket in buckets.items():
        over = sorted(i for i, ids in per_bucket.items() if len(ids) >= p.crowd_min_people)
        if not over:
            continue
        # Group consecutive over-threshold buckets into runs.
        runs: list[list[int]] = []
        for i in over:
            if runs and i == runs[-1][-1] + 1:
                runs[-1].append(i)
            else:
                runs.append([i])

        cam_tracks = [t for t in persons if str(t["camera_id"]) == cam]
        for run in runs:
            if len(run) < p.crowd_sustained_buckets:
                continue
            peak_bucket = max(run, key=lambda i: len(per_bucket[i]))
            peak = len(per_bucket[peak_bucket])
            member_ids = set().union(*(per_bucket[i] for i in run))
            members = [t for t in cam_tracks if str(t["id"]) in member_ids]
            if not members:
                continue
            started = min(t["first_seen"] for t in members)
            ended = max(t["last_seen"] for t in members)
            representative = max(members, key=lambda t: t["detection_count"])

            out.append(EventDraft(
                camera_id=cam,
                event_type="crowd",
                severity="high" if peak >= 2 * p.crowd_min_people else "medium",
                label=f"Crowd: {peak} people",
                detection_id=str(representative["best_detection_id"])
                    if representative.get("best_detection_id") else None,
                started_at=started,
                ended_at=ended,
                pts_ms=bucket_span[cam][run[0]][0] * 1000.0,
                confidence=min(0.93, 0.55 + 0.05 * (peak - p.crowd_min_people)),
                thumbnail_key=representative.get("thumbnail_key"),
                metrics={
                    "peak_people": peak,
                    "threshold": p.crowd_min_people,
                    "bucket_s": p.crowd_bucket_s,
                    "sustained_buckets": len(run),
                    "distinct_tracks": len(member_ids),
                    "per_bucket": [len(per_bucket[i]) for i in run],
                },
            # Keyed on the run's first bucket, not on wall-clock: re-running the
            # engine over the same footage recomputes the same bucket index.
            ).with_key(cam, run[0], p.crowd_bucket_s))
    return out


# ── 20. abandoned object ─────────────────────────────────────────────────────

def detect_abandoned_objects(tracks: list[dict], p: RuleParams) -> list[EventDraft]:
    """A static object that stayed put with nobody near it.

    The person-clearance test is what separates a threat from luggage: a bag on
    a platform with its owner beside it is not an event, and a rule without that
    test fires on every waiting passenger.

    Requires the pipeline to emit object classes (see :data:`OBJECT_CLASSES`).
    It filters YOLO to vehicles and people today, so this detector returns
    nothing until that class set is widened — which is a two-line pipeline
    change, not a rewrite here.
    """
    objects = [t for t in tracks if t["class"] in OBJECT_CLASSES]
    if not objects:
        return []
    persons = [t for t in tracks if t["class"] in PERSON_CLASSES]

    out: list[EventDraft] = []
    for t in objects:
        dwell = float(t["duration_s"] or 0.0)
        spread = float(t["max_spread_norm"] or 0.0)
        if dwell < p.abandoned_min_dwell_s or spread > p.abandoned_max_spread_norm:
            continue
        pts = _path(t)
        if not pts:
            continue
        ox = sum(q["x"] for q in pts) / len(pts)
        oy = sum(q["y"] for q in pts) / len(pts)

        # Nearest approach by any person while the object was present.
        nearest = float("inf")
        nearest_track = None
        for pt in persons:
            if str(pt["camera_id"]) != str(t["camera_id"]):
                continue
            if pt["last_seen"] < t["first_seen"] or pt["first_seen"] > t["last_seen"]:
                continue
            for q in _path(pt):
                d = math.hypot(q["x"] - ox, q["y"] - oy)
                if d < nearest:
                    nearest, nearest_track = d, str(pt["id"])
        if nearest <= p.abandoned_person_clearance_norm:
            continue

        out.append(EventDraft(
            camera_id=str(t["camera_id"]),
            event_type="abandoned_object",
            severity="high",
            label=f"Unattended {t['class']} — {int(dwell)}s",
            track_uuid=str(t["id"]),
            detection_id=str(t["best_detection_id"]) if t.get("best_detection_id") else None,
            started_at=t["first_seen"],
            ended_at=t["last_seen"],
            pts_ms=t.get("first_pts_ms"),
            confidence=min(0.88, 0.55 + 0.1 * (dwell / p.abandoned_min_dwell_s - 1)),
            thumbnail_key=t.get("thumbnail_key"),
            metrics={
                "dwell_s": round(dwell, 1),
                "max_spread_norm": round(spread, 4),
                "nearest_person_norm": None if nearest == float("inf") else round(nearest, 4),
                "nearest_person_track": nearest_track,
                "clearance_threshold_norm": p.abandoned_person_clearance_norm,
                "class": t["class"],
            },
        ).with_key(t["id"]))
    return out


# ── 17. accident candidate ───────────────────────────────────────────────────

def _pair_contact(a_pts: list[dict], b_pts: list[dict], p: RuleParams) -> Optional[dict]:
    """Find the moment two tracks' boxes *began* to overlap, if they ever did.

    Walks both paths on a shared clock rather than by index — the two tracks were
    sampled on the same frames but may have started at different ones, so index
    ``i`` of one is not index ``i`` of the other.

    ``clock`` is the **onset** of overlap — the first sample where IoU reaches
    ``accident_min_iou`` — not the moment IoU peaked. That distinction is the
    whole point of this function, because the caller splits each path into
    before/after at ``clock`` and measures a velocity change across the split.

    In the case this rule exists for, two vehicles collide and then stay put, so
    they remain overlapped for as long as they are both in frame and IoU is
    roughly flat across that whole period. Taking the argmax then lands the split
    at an arbitrary point *inside* the post-contact period, which corrupts both
    measurements at once and in the same direction: the "after" window loses the
    seconds before the peak, so the stationary period reads shorter than it was,
    and the "before" window gains them, so the pre-contact speed reads slower
    than it was. Both errors push toward rejection, so the rule silently misses
    exactly the sustained-overlap collisions it is meant to catch — a 22-second
    two-vehicle stop measured 7.0 s of stillness against an 8 s floor.

    Peak IoU is still returned as ``iou``: it is the better evidence of how hard
    the boxes actually intersected, and the caller scores confidence with it.
    """
    a_clock = [(_clock(q), q) for q in a_pts]
    b_clock = [(_clock(q), q) for q in b_pts]
    a_clock = [(c, q) for c, q in a_clock if c is not None]
    b_clock = [(c, q) for c, q in b_clock if c is not None]
    if not a_clock or not b_clock:
        return None

    peak: Optional[dict] = None
    onset: Optional[dict] = None
    j = 0
    for ca, qa in a_clock:
        while j + 1 < len(b_clock) and abs(b_clock[j + 1][0] - ca) <= abs(b_clock[j][0] - ca):
            j += 1
        cb, qb = b_clock[j]
        if abs(cb - ca) > p.accident_time_tolerance_s:
            continue
        iou = bbox_iou(_box(qa), _box(qb))
        moment = {"iou": iou, "clock": (ca + cb) / 2.0, "a": qa, "b": qb}
        if peak is None or iou > peak["iou"]:
            peak = moment
        if onset is None and iou >= p.accident_min_iou:
            onset = moment
    if peak is None:
        return None
    if onset is None:
        # Never reached the threshold. Return the peak so the caller's own
        # ``iou < accident_min_iou`` check rejects it, rather than reporting a
        # contact time for an overlap that did not happen.
        return peak
    return {"iou": peak["iou"], "clock": onset["clock"],
            "a": onset["a"], "b": onset["b"], "peak_clock": peak["clock"]}


def detect_accident_candidates(tracks: list[dict], p: RuleParams) -> list[EventDraft]:
    """Two vehicles that overlapped and then both stopped.

    This is a heuristic, and the event type says ``candidate`` because that is
    what it is. Three conditions in sequence, which is what makes it survivable:

    1. two vehicle tracks on the same camera overlap in image space;
    2. at least one was moving properly before that moment;
    3. both are near-stationary for several seconds after it.

    Overlap alone is worthless from a monocular camera — vehicles in adjacent
    lanes overlap constantly at a shallow angle. The *before-and-after velocity
    change* is what carries the signal, and requiring it of **both** tracks is
    what rejects the common false positive of one vehicle passing another that
    happened to be parked.

    No claim is made about injury, fault, or whether a collision occurred. It
    surfaces a 10-second clip for a human to look at, which is the only honest
    thing an uncalibrated bounding-box heuristic can offer.
    """
    by_camera: dict[str, list[dict]] = {}
    for t in tracks:
        if t["class"] in VEHICLE_CLASSES:
            by_camera.setdefault(str(t["camera_id"]), []).append(t)

    out: list[EventDraft] = []
    for cam, cam_tracks in by_camera.items():
        cam_tracks.sort(key=lambda t: t["first_seen"])
        for i, ta in enumerate(cam_tracks):
            pa = _path(ta)
            if len(pa) < 6:
                continue
            for tb in cam_tracks[i + 1:]:
                # Time-overlap prefilter: sorted by first_seen, so once a
                # candidate starts after ta ended, every later one does too.
                if tb["first_seen"] > ta["last_seen"]:
                    break
                pb = _path(tb)
                if len(pb) < 6:
                    continue

                contact = _pair_contact(pa, pb, p)
                if not contact or contact["iou"] < p.accident_min_iou:
                    continue

                ck = contact["clock"]
                a_before = [q for q in pa if (_clock(q) or 0) < ck]
                a_after = [q for q in pa if (_clock(q) or 0) >= ck]
                b_before = [q for q in pb if (_clock(q) or 0) < ck]
                b_after = [q for q in pb if (_clock(q) or 0) >= ck]
                if len(a_after) < 3 or len(b_after) < 3:
                    continue

                sa_before, sb_before = _mean_speed(a_before), _mean_speed(b_before)
                sa_after, sb_after = _mean_speed(a_after), _mean_speed(b_after)
                if max(sa_before, sb_before) < p.accident_min_speed_before:
                    continue
                if sa_after > p.accident_max_speed_after or sb_after > p.accident_max_speed_after:
                    continue

                def _span(points: list[dict]) -> float:
                    c0, c1 = _clock(points[0]), _clock(points[-1])
                    return (c1 - c0) if (c0 is not None and c1 is not None) else 0.0

                still_s = min(_span(a_after), _span(b_after))
                if still_s < p.accident_min_stationary_s:
                    continue

                # Weight the two independent pieces of evidence: how much the
                # boxes overlapped, and how completely the motion stopped.
                overlap_term = min(1.0, contact["iou"] / max(p.accident_min_iou, 1e-6) / 3.0)
                stop_term = min(1.0, still_s / (3 * p.accident_min_stationary_s))
                conf = round(min(0.85, 0.35 + 0.3 * overlap_term + 0.25 * stop_term), 4)

                started = max(ta["first_seen"], tb["first_seen"])
                out.append(EventDraft(
                    camera_id=cam,
                    event_type="accident_candidate",
                    severity="critical" if conf >= 0.7 else "high",
                    label=f"Possible collision: {ta['class']} + {tb['class']}",
                    track_uuid=str(ta["id"]),
                    detection_id=contact["a"].get("d"),
                    started_at=started,
                    ended_at=max(ta["last_seen"], tb["last_seen"]),
                    pts_ms=contact["a"].get("pts_ms"),
                    confidence=conf,
                    thumbnail_key=ta.get("thumbnail_key"),
                    plate_text=ta.get("plate_text") or tb.get("plate_text"),
                    metrics={
                        "peak_iou": round(contact["iou"], 4),
                        "other_track": str(tb["id"]),
                        "other_class": tb["class"],
                        "other_plate": tb.get("plate_text"),
                        "speed_before": [round(sa_before, 5), round(sb_before, 5)],
                        "speed_after": [round(sa_after, 5), round(sb_after, 5)],
                        "stationary_after_s": round(still_s, 1),
                        "heuristic": "box_overlap + bilateral_velocity_drop",
                        "not_a_classifier": True,
                    },
                # Track pair, order-normalised so (a,b) and (b,a) are one event.
                ).with_key(*sorted([str(ta["id"]), str(tb["id"])])))
    return out


# ── orchestration ────────────────────────────────────────────────────────────

_TRACK_COLS = """
    id, camera_id, track_id, session_key, class, dominant_color, plate_text,
    plate_confidence, first_seen, last_seen, first_pts_ms, last_pts_ms,
    duration_s, detection_count, best_detection_id, thumbnail_key, path,
    heading_deg, displacement_norm, max_spread_norm, source
"""


def load_tracks(
    conn,
    camera_id: Optional[str] = None,
    since=None,
    session_key: Optional[str] = None,
) -> list[dict]:
    """Load folded tracks for the rule engine. Embeddings are not selected —
    no rule here needs one, and 512 floats per track is the difference between
    a fast query and a slow one."""
    clauses, params = ["TRUE"], []
    if camera_id:
        clauses.append("camera_id = %s")
        params.append(camera_id)
    if since is not None:
        clauses.append("last_seen >= %s")
        params.append(since)
    if session_key:
        clauses.append("session_key = %s")
        params.append(session_key)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TRACK_COLS} FROM tracks WHERE {' AND '.join(clauses)} "
            "ORDER BY camera_id, first_seen",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def load_zones(conn, camera_id: Optional[str] = None) -> list[dict]:
    clauses, params = ["active"], []
    if camera_id:
        clauses.append("camera_id = %s")
        params.append(camera_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, camera_id, name, kind, polygon, direction_deg, "
            "tolerance_deg, params, classes FROM zones "
            f"WHERE {' AND '.join(clauses)}",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


# Rule name → (callable, needs_zones). Named so the CLI can enable a subset and
# so a rule that turns out to be noisy on real footage can be switched off in
# one place rather than commented out of a pipeline.
RULES: dict[str, tuple[Any, bool]] = {
    "loitering": (detect_loitering, False),
    "stopped_vehicle": (detect_stopped_vehicles, False),
    "wrong_way": (detect_wrong_way, True),
    "zone_intrusion": (detect_zone_intrusion, True),
    "crowd": (detect_crowd, False),
    "abandoned_object": (detect_abandoned_objects, False),
    "accident_candidate": (detect_accident_candidates, False),
}


def run_rules(
    conn,
    camera_id: Optional[str] = None,
    since=None,
    session_key: Optional[str] = None,
    enabled: Optional[Iterable[str]] = None,
    params: Optional[RuleParams] = None,
    dry_run: bool = False,
) -> dict:
    """Run the enabled rules over folded tracks and write the events.

    Returns a per-rule report: how many events each rule proposed, how many were
    new, and how many were suppressed as already-known. The suppressed count is
    the interesting one during tuning — a rule that proposes 400 events of which
    0 are new is stable; one that proposes 400 new every run has a
    non-deterministic dedupe key and is broken.
    """
    p = params or RuleParams()
    names = list(enabled) if enabled else list(RULES)
    unknown = [n for n in names if n not in RULES]
    if unknown:
        raise ValueError(f"unknown rule(s): {', '.join(unknown)}")

    tracks = load_tracks(conn, camera_id, since, session_key)
    if not tracks:
        return {"tracks": 0, "rules": {}, "inserted": 0, "duplicate": 0}
    zones = load_zones(conn, camera_id) if any(RULES[n][1] for n in names) else []

    report: dict[str, dict] = {}
    total_new = total_dupe = 0
    for name in names:
        fn, needs_zones = RULES[name]
        try:
            drafts = fn(tracks, zones, p) if needs_zones else fn(tracks, p)
        except Exception:
            log.exception("rule %s failed — skipping it, other rules continue", name)
            report[name] = {"proposed": 0, "new": 0, "duplicate": 0, "error": True}
            continue
        if dry_run:
            report[name] = {"proposed": len(drafts), "new": 0, "duplicate": 0}
            continue
        new, dupe = write_events(conn, drafts)
        conn.commit()
        report[name] = {"proposed": len(drafts), "new": new, "duplicate": dupe}
        total_new += new
        total_dupe += dupe

    return {
        "tracks": len(tracks),
        "zones": len(zones),
        "rules": report,
        "inserted": total_new,
        "duplicate": total_dupe,
        "dry_run": dry_run,
    }
