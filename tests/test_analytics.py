"""Tests for the track-level analytics layer — ANPR consensus, the fold, rules.

These deliberately require no database, no GPU and no camera feed. The detectors
are pure functions over track dicts, which is why they can be tested at all: the
alternative (assert against a live pipeline) would make the thresholds
untestable and therefore untuned.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from ml.analytics.anpr import consensus_plate, plate_similarity
from ml.analytics.geometry import (
    angle_delta,
    bbox_ground_point,
    bbox_iou,
    heading_deg,
    point_in_polygon,
)
from ml.analytics.rules import (
    RuleParams,
    detect_abandoned_objects,
    detect_accident_candidates,
    detect_crowd,
    detect_loitering,
    detect_stopped_vehicles,
    detect_wrong_way,
    detect_zone_intrusion,
)
from ml.analytics.tracks import summarise_track

T0 = datetime(2026, 9, 5, 14, 0, 0, tzinfo=timezone.utc)
CAM = "11111111-1111-1111-1111-111111111111"


# ── helpers ──────────────────────────────────────────────────────────────────

def det(i, x, y, w=120, h=90, cls="car", conf=0.9, color="white",
        plate=None, pconf=None, dt=0.5):
    """One detection row as the DB would hand it back."""
    return {
        "id": f"00000000-0000-0000-0000-{i:012d}", "camera_id": CAM, "track_id": 1,
        "session_key": "live", "class": cls, "confidence": conf,
        "dominant_color": color, "plate_text": plate, "plate_confidence": pconf,
        "bbox": {"x1": x, "y1": y, "x2": x + w, "y2": y + h},
        "ts": T0 + timedelta(seconds=i * dt), "pts_ms": i * dt * 1000.0,
        "thumbnail_key": f"t/{i}.jpg", "frame_w": 1920, "frame_h": 1080,
        "source": "live",
    }


def make_track(tid, cls, points, *, plate=None, dt=0.5, w=120, h=90,
               camera=CAM, size=(1920, 1080)):
    """Build a track row by folding synthetic detections, as the real pipeline
    would — so the tests exercise summarise_track and the rules together rather
    than hand-crafting track fields the fold might never produce."""
    rows = []
    for i, (x, y) in enumerate(points):
        r = det(i, x, y, w=w, h=h, cls=cls, dt=dt,
                plate=plate if (plate and i % 3 == 0) else None,
                pconf=0.7 if plate else None)
        r["camera_id"] = camera
        r["track_id"] = tid
        rows.append(r)
    s = summarise_track(rows, size)
    s["id"] = f"aaaaaaaa-0000-0000-0000-{tid:012d}"
    s["camera_id"] = camera
    s["track_id"] = tid
    s["session_key"] = "live"
    return s


def line(x0, y0, x1, y1, n):
    return [(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1)) for i in range(n)]


def jitter(x, y, n, r=4):
    return [(x + r * math.cos(i), y + r * math.sin(i * 1.7)) for i in range(n)]


# ── geometry ─────────────────────────────────────────────────────────────────

def test_ground_point_is_bottom_centre():
    assert bbox_ground_point({"x1": 100, "y1": 200, "x2": 300, "y2": 400}) == (200.0, 400.0)


def test_point_in_polygon_concave():
    # An L-shape: the notch must read as outside.
    poly = [[0, 0], [1, 0], [1, 0.4], [0.4, 0.4], [0.4, 1], [0, 1]]
    assert point_in_polygon((0.2, 0.2), poly)
    assert point_in_polygon((0.8, 0.2), poly)
    assert not point_in_polygon((0.8, 0.8), poly)


def test_heading_is_compass_style():
    assert heading_deg((0.5, 0.5), (0.5, 0.1)) == pytest.approx(0.0, abs=1e-6)    # up-frame
    assert heading_deg((0.5, 0.5), (0.9, 0.5)) == pytest.approx(90.0, abs=1e-6)   # right
    assert heading_deg((0.5, 0.5), (0.5, 0.9)) == pytest.approx(180.0, abs=1e-6)  # down


def test_angle_delta_wraps():
    assert angle_delta(10, 350) == pytest.approx(20.0)
    assert angle_delta(0, 180) == pytest.approx(180.0)


def test_bbox_iou():
    a = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
    assert bbox_iou(a, a) == pytest.approx(1.0)
    assert bbox_iou(a, {"x1": 20, "y1": 20, "x2": 30, "y2": 30}) == 0.0
    assert bbox_iou(a, {"x1": 5, "y1": 0, "x2": 15, "y2": 10}) == pytest.approx(1 / 3)


# ── multi-frame ANPR ─────────────────────────────────────────────────────────

def test_consensus_recovers_plate_no_single_read_got_right():
    """The core claim of the ANPR engine: eleven wrong reads beat the best one."""
    reads = ["GJ0IAB1Z34", "GJ01A81234", "6J01AB1Z34", "GJ0lAB1234",
             "GJ01AB1Z34", "GJ01A81Z34", "J01AB1234", "GJ01AB1Z3A"]
    assert "GJ01AB1234" not in reads          # no read is individually correct
    plate, conf, n, votes = consensus_plate([(r, 0.7) for r in reads])
    assert plate == "GJ01AB1234"
    assert n == len(reads)
    assert votes["format_valid"] is True
    assert 0.3 < conf < 0.9


def test_consensus_snaps_runner_up_when_argmax_is_illegal():
    """The argmax may not be a legal plate; the fix is usually the runner-up."""
    reads = ["GJ0IAB1Z34", "GJ01A81234", "6J01AB1Z34", "GJ0lAB1234", "GJ01AB1234",
             "GJ01AB1Z34", "GJ01A81Z34", "J01AB1234"]
    plate, _, _, votes = consensus_plate([(r, 0.7) for r in reads])
    assert votes["raw_consensus"] == "GJ01AB1Z34"     # argmax: not a legal plate
    assert votes["snap_method"] == "beam_snap"
    assert plate == "GJ01AB1234"
    assert votes["snap_cost"] < 0.5                   # a cheap trade, not invention


def test_consensus_refuses_to_invent_a_plate_from_noise():
    plate, conf, _, votes = consensus_plate([(r, 0.6) for r in
                                             ["XQ7WZZ", "QW9ZXK1", "ZX8QW7K", "WQ8XZ9K"]])
    assert votes["format_valid"] is False
    assert votes["snap_method"] == "unsnapped"
    assert conf < 0.3


def test_single_read_is_capped():
    plate, conf, n, votes = consensus_plate([("GJ01AB1234", 0.99)])
    assert (plate, n) == ("GJ01AB1234", 1)
    assert conf <= 0.28, "one read is a lead, never a conclusion"


def test_confidence_grows_with_evidence():
    few = consensus_plate([("GJ01AB1234", 0.8)] * 2)[1]
    many = consensus_plate([("GJ01AB1234", 0.8)] * 10)[1]
    assert many > few


def test_plate_similarity_is_confusion_aware():
    assert plate_similarity("GJ01AB1234", "GJ0IAB1Z34") > 0.9   # glyph confusions
    assert plate_similarity("GJ01AB1234", "GJ01AB1235") > 0.85  # one real digit off
    assert plate_similarity("GJ01AB1234", "MH12XY9876") < 0.2
    assert plate_similarity("GJ01AB1234", None) == 0.0


# ── the fold ─────────────────────────────────────────────────────────────────

def test_fold_votes_class_by_confidence_not_frame_count():
    """Six low-confidence frames must not outvote four clear ones."""
    rows = ([det(i, 100 + i * 10, 800, cls="truck", conf=0.42) for i in range(6)] +
            [det(i, 160 + i * 10, 800, cls="car", conf=0.93) for i in range(6, 10)])
    s = summarise_track(rows, (1920, 1080))
    assert s["class"] == "car"
    assert s["class_votes"]["car"] > s["class_votes"]["truck"]


def test_fold_uses_pts_not_wallclock_for_duration():
    """Archive footage is processed faster than real time; pts_ms is the truth."""
    rows = [det(i, 100 + i * 10, 800) for i in range(20)]
    for r in rows:                       # inference took 1s total…
        r["ts"] = T0 + timedelta(milliseconds=rows.index(r) * 50)
    s = summarise_track(rows, (1920, 1080))
    assert s["duration_s"] == pytest.approx(9.5)   # …but the video spans 9.5s


def test_fold_separates_pacing_from_standing_still():
    """Displacement can't tell them apart; spread can — that's why we store it."""
    still = summarise_track([det(i, 900, 700) for i in range(40)], (1920, 1080))
    pacing = summarise_track(
        [det(i, 900 + int(200 * math.sin(i / 6)), 700) for i in range(40)], (1920, 1080))
    assert still["displacement_norm"] == pytest.approx(0.0, abs=1e-4)
    assert pacing["displacement_norm"] < 0.05          # net travel ≈ nothing
    assert pacing["max_spread_norm"] > 5 * still["max_spread_norm"]


def test_fold_downsamples_path_but_keeps_the_ends():
    rows = [det(i, 100 + i * 3, 800) for i in range(500)]
    s = summarise_track(rows, (1920, 1080))
    assert len(s["path"]) <= 120
    assert s["path"][0]["d"] == rows[0]["id"]
    assert s["path"][-1]["d"] == rows[-1]["id"]


def test_fold_path_carries_normalised_box_size():
    s = summarise_track([det(0, 100, 200, w=192, h=108)], (1920, 1080))
    p = s["path"][0]
    assert (p["w"], p["h"]) == (0.1, 0.1)


def test_fold_picks_highest_confidence_frame_as_best():
    rows = [det(i, 100 + i * 10, 800, conf=0.5 + 0.01 * i) for i in range(20)]
    s = summarise_track(rows, (1920, 1080))
    assert s["best_detection_id"] == rows[-1]["id"]
    assert s["thumbnail_key"] == rows[-1]["thumbnail_key"]


# ── rules: loitering & stopped vehicle ───────────────────────────────────────

P = RuleParams()


def test_loitering_fires_on_a_person_who_stayed():
    # 400 samples × 0.5 s = 200 s in a small circle.
    t = make_track(1, "person", jitter(900, 700, 400, r=6))
    events = detect_loitering([t], P)
    assert len(events) == 1
    assert events[0].event_type == "loitering"
    assert events[0].metrics["dwell_s"] >= P.loiter_min_dwell_s


def test_loitering_ignores_someone_walking_through_slowly():
    t = make_track(2, "person", line(100, 700, 1800, 720, 400))
    assert detect_loitering([t], P) == []


def test_loitering_ignores_a_brief_stop():
    t = make_track(3, "person", jitter(900, 700, 40))       # 20 s
    assert detect_loitering([t], P) == []


def test_loitering_ignores_vehicles():
    t = make_track(4, "car", jitter(900, 700, 400, r=6))
    assert detect_loitering([t], P) == []


def test_stopped_vehicle_fires_and_carries_its_plate():
    t = make_track(5, "car", jitter(900, 700, 500, r=3), plate="GJ01AB1234")
    events = detect_stopped_vehicles([t], P)
    assert len(events) == 1
    assert events[0].plate_text == "GJ01AB1234"
    assert events[0].metrics["dwell_s"] >= P.stopped_min_dwell_s


def test_stopped_vehicle_ignores_traffic_light_queue():
    t = make_track(6, "car", jitter(900, 700, 200, r=3))     # 100 s < 180 s
    assert detect_stopped_vehicles([t], P) == []


def test_thresholds_are_overridable_without_a_redeploy():
    t = make_track(7, "person", jitter(900, 700, 100, r=6))  # 50 s
    assert detect_loitering([t], P) == []
    assert len(detect_loitering([t], P.merged({"loiter_min_dwell_s": 45}))) == 1


# ── rules: zones ─────────────────────────────────────────────────────────────

def zone(kind, polygon, **kw):
    z = {
        "id": f"bbbbbbbb-0000-0000-0000-{abs(hash(kind)) % 10**12:012d}",
        "camera_id": CAM, "name": f"{kind} zone", "kind": kind,
        "polygon": polygon, "direction_deg": None, "tolerance_deg": 75.0,
        "params": {}, "classes": [],
    }
    z.update(kw)
    return z


RIGHT_HALF = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]


def test_zone_intrusion_fires_when_the_track_enters():
    t = make_track(10, "person", line(1200, 700, 1700, 720, 40))   # x/1920 > 0.5
    events = detect_zone_intrusion([t], [zone("restricted", RIGHT_HALF)], P)
    assert len(events) == 1
    assert events[0].metrics["fraction_inside"] == 1.0
    assert events[0].severity == "high"                            # person


def test_zone_intrusion_ignores_a_track_that_stays_outside():
    t = make_track(11, "person", line(100, 700, 800, 720, 40))     # x/1920 < 0.5
    assert detect_zone_intrusion([t], [zone("restricted", RIGHT_HALF)], P) == []


def test_zone_intrusion_respects_the_class_filter():
    t = make_track(12, "car", line(1200, 700, 1700, 720, 40))
    zones = [zone("restricted", RIGHT_HALF, classes=["person"])]
    assert detect_zone_intrusion([t], zones, P) == []


def test_zone_intrusion_ignores_boundary_jitter():
    """A single point inside is box noise, not an intrusion."""
    # Ground point is bottom-centre, so the box x is 60 px left of what counts.
    # One sample lands inside the zone, thirty sit well outside it.
    pts = [(950, 700)] + [(700, 700)] * 30
    t = make_track(13, "person", pts)
    assert detect_zone_intrusion([t], [zone("restricted", RIGHT_HALF)], P) == []


def test_zone_intrusion_only_looks_at_the_camera_that_owns_the_zone():
    other = "22222222-2222-2222-2222-222222222222"
    t = make_track(14, "person", line(1200, 700, 1700, 720, 40), camera=other)
    assert detect_zone_intrusion([t], [zone("restricted", RIGHT_HALF)], P) == []


# ── rules: wrong way ─────────────────────────────────────────────────────────

FULL_FRAME = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def test_wrong_way_fires_against_the_permitted_heading():
    # Zone permits travel to the right (90°); this track goes left (270°).
    t = make_track(20, "car", line(1700, 700, 200, 700, 40))
    z = zone("direction", FULL_FRAME, direction_deg=90.0)
    events = detect_wrong_way([t], [z], P)
    assert len(events) == 1
    assert events[0].metrics["angle_delta_deg"] == pytest.approx(180.0, abs=2)
    assert events[0].severity == "high"


def test_wrong_way_silent_when_travelling_correctly():
    t = make_track(21, "car", line(200, 700, 1700, 700, 40))
    z = zone("direction", FULL_FRAME, direction_deg=90.0)
    assert detect_wrong_way([t], [z], P) == []


def test_wrong_way_ignores_a_track_that_barely_moved():
    """Heading over a 20-pixel path is tracker jitter, not a direction."""
    t = make_track(22, "car", line(900, 700, 880, 700, 40))
    z = zone("direction", FULL_FRAME, direction_deg=90.0)
    assert detect_wrong_way([t], [z], P) == []


def test_wrong_way_tolerance_permits_a_diagonal():
    t = make_track(23, "car", line(200, 700, 1700, 200, 40))   # up-and-right ≈ 71°
    z = zone("direction", FULL_FRAME, direction_deg=90.0, tolerance_deg=75.0)
    assert detect_wrong_way([t], [z], P) == []
    tight = zone("direction", FULL_FRAME, direction_deg=90.0, tolerance_deg=10.0)
    assert len(detect_wrong_way([t], [tight], P)) == 1


# ── rules: crowd ─────────────────────────────────────────────────────────────

def crowd_tracks(n_people, n_samples=90, tid_base=100):
    """n people walking through, each present for n_samples × 0.5 s."""
    return [make_track(tid_base + k, "person",
                       line(100 + k * 20, 700, 400 + k * 20, 720, n_samples))
            for k in range(n_people)]


def test_crowd_fires_on_many_distinct_people():
    events = detect_crowd(crowd_tracks(20), P)
    assert len(events) >= 1
    assert events[0].metrics["peak_people"] >= P.crowd_min_people


def test_crowd_counts_people_not_detections():
    """One person for 600 frames must never look like a crowd."""
    long_stay = make_track(200, "person", jitter(900, 700, 600))
    assert long_stay["detection_count"] == 600
    assert detect_crowd([long_stay], P) == []


def test_crowd_requires_the_count_to_be_sustained():
    """20 people for a single 30-second bucket is a bus, not an event."""
    brief = [make_track(300 + k, "person", line(100 + k * 20, 700, 300 + k * 20, 720, 20))
             for k in range(20)]                       # 10 s each
    assert detect_crowd(brief, P) == []


def test_crowd_ignores_vehicles():
    cars = [make_track(400 + k, "car", line(100 + k * 20, 700, 400 + k * 20, 720, 90))
            for k in range(20)]
    assert detect_crowd(cars, P) == []


# ── rules: abandoned object ──────────────────────────────────────────────────

def test_abandoned_object_fires_when_nobody_is_near():
    bag = make_track(500, "backpack", jitter(300, 900, 700, r=2), w=40, h=40)
    events = detect_abandoned_objects([bag], P)
    assert len(events) == 1
    assert events[0].severity == "high"


def test_abandoned_object_is_silent_when_the_owner_is_beside_it():
    bag = make_track(501, "backpack", jitter(300, 900, 700, r=2), w=40, h=40)
    owner = make_track(502, "person", jitter(330, 900, 700, r=6))
    assert detect_abandoned_objects([bag, owner], P) == []


def test_abandoned_object_returns_nothing_for_classes_the_pipeline_emits_today():
    """Documented limitation, asserted so it can't rot silently: the pipeline
    filters YOLO to vehicles and people, so this detector is inert until that
    class set is widened."""
    vehicles = [make_track(503, "car", jitter(300, 900, 700, r=2))]
    assert detect_abandoned_objects(vehicles, P) == []


# ── rules: accident candidate ────────────────────────────────────────────────

def test_accident_candidate_fires_on_overlap_then_bilateral_stop():
    # A drives right and stops at x=900; B drives left into the same place and
    # stops there too. Both boxes overlap and neither moves afterwards.
    a_pts = line(300, 700, 900, 700, 30) + [(900, 700)] * 60
    b_pts = line(1500, 700, 940, 700, 30) + [(940, 700)] * 60
    a = make_track(600, "car", a_pts)
    b = make_track(601, "truck", b_pts)
    events = detect_accident_candidates([a, b], P)
    assert len(events) == 1
    e = events[0]
    assert e.event_type == "accident_candidate"
    assert e.metrics["peak_iou"] >= P.accident_min_iou
    assert e.metrics["stationary_after_s"] >= P.accident_min_stationary_s
    assert e.metrics["not_a_classifier"] is True


def test_accident_candidate_ignores_a_car_passing_a_parked_one():
    """The most common false positive: overlap without a bilateral stop."""
    parked = make_track(602, "car", [(900, 700)] * 90)
    passing = make_track(603, "car", line(200, 700, 1700, 700, 90))
    assert detect_accident_candidates([parked, passing], P) == []


def test_accident_candidate_ignores_two_parked_vehicles():
    """Boxes may overlap all day; nothing was ever moving."""
    a = make_track(604, "car", [(900, 700)] * 90)
    b = make_track(605, "car", [(940, 700)] * 90)
    assert detect_accident_candidates([a, b], P) == []


def test_accident_candidate_ignores_adjacent_lane_traffic():
    a = make_track(606, "car", line(200, 700, 1700, 700, 90))
    b = make_track(607, "car", line(200, 820, 1700, 820, 90))
    assert detect_accident_candidates([a, b], P) == []


def test_accident_candidate_dedupe_key_is_pair_order_independent():
    a_pts = line(300, 700, 900, 700, 30) + [(900, 700)] * 60
    b_pts = line(1500, 700, 940, 700, 30) + [(940, 700)] * 60
    a = make_track(608, "car", a_pts)
    b = make_track(609, "truck", b_pts)
    k1 = detect_accident_candidates([a, b], P)[0].dedupe_key
    k2 = detect_accident_candidates([b, a], P)[0].dedupe_key
    assert k1 == k2, "re-running with tracks in another order must not re-alert"


# ── idempotency ──────────────────────────────────────────────────────────────

def test_dedupe_keys_are_stable_across_runs():
    """The property the whole re-run story rests on: keys must contain nothing
    that shifts between invocations."""
    t = make_track(700, "person", jitter(900, 700, 400, r=6))
    first = [e.dedupe_key for e in detect_loitering([t], P)]
    second = [e.dedupe_key for e in detect_loitering([t], P)]
    assert first == second and first


def test_every_draft_has_a_dedupe_key():
    tracks = [
        make_track(800, "person", jitter(900, 700, 400, r=6)),
        make_track(801, "car", jitter(400, 800, 500, r=3), plate="GJ01AB1234"),
        make_track(802, "car", line(1700, 700, 200, 700, 40)),
    ] + crowd_tracks(20, tid_base=900)
    zones = [zone("restricted", RIGHT_HALF),
             zone("direction", FULL_FRAME, direction_deg=90.0)]
    drafts = (detect_loitering(tracks, P) + detect_stopped_vehicles(tracks, P)
              + detect_wrong_way(tracks, zones, P) + detect_zone_intrusion(tracks, zones, P)
              + detect_crowd(tracks, P) + detect_abandoned_objects(tracks, P)
              + detect_accident_candidates(tracks, P))
    assert drafts, "the fixture should trigger several rules"
    assert all(d.dedupe_key for d in drafts)
    assert len({d.dedupe_key for d in drafts}) == len(drafts), "keys must be unique"


# ── Plate trust tiers ────────────────────────────────────────────────────────
# The bands come from a measurement (scripts/export_plate_labels.py) and these
# pin the decisions that measurement implies, so that a later retune has to
# change the test deliberately rather than drift past it.

def test_trust_actionable_only_above_measured_threshold():
    from ml.plate_trust import plate_trust
    assert plate_trust(0.66, read_count=10).tier == "actionable"
    assert plate_trust(0.50, read_count=10).tier == "actionable"
    assert plate_trust(0.49, read_count=10).tier == "verify"


def test_trust_lead_below_the_band_that_was_never_right():
    from ml.plate_trust import plate_trust
    assert plate_trust(0.29, read_count=8).tier == "lead"
    assert plate_trust(0.0, read_count=8).tier == "lead"
    assert plate_trust(None, read_count=8).tier == "lead"


def test_single_read_is_never_actionable():
    """One frame is a lead however confident the OCR claims to be."""
    from ml.plate_trust import plate_trust
    assert plate_trust(0.99, read_count=1).tier == "lead"


def test_short_tail_cannot_be_actionable():
    """A confidently truncated plate is the dangerous case: it looks right and
    searches a registry for a registration that was never issued."""
    from ml.plate_trust import plate_trust
    assert plate_trust(0.80, read_count=10, truncated=True).tier == "verify"
    assert plate_trust(0.80, read_count=10, truncated=False).tier == "actionable"


def test_describe_is_json_serialisable_and_cites_its_measurement():
    import json
    from ml.plate_trust import describe
    d = describe(0.66, read_count=10)
    json.dumps(d)
    assert d["tier"] == "actionable"
    assert "2 of 2" in d["measured"]


# ── ByteTrack id reuse ───────────────────────────────────────────────────────
# A track number is unique within a lifetime, not within a run: once a track has
# been lost for longer than the tracker's buffer the number is recycled, and the
# fold used to weld the two vehicles into one row. The case that surfaced it is
# in test_splits_the_track_that_was_two_cars.

def _at(sec: float) -> dict:
    from datetime import datetime, timedelta, timezone
    return {"ts": datetime(2026, 9, 5, 0, 10, 8, tzinfo=timezone.utc) + timedelta(seconds=sec),
            "mark": sec}


def test_splits_the_track_that_was_two_cars():
    """cam09, 5 Sept. Four frames of GJ11AB4579, a 56-second hole, then two of
    GJ01RN7172 — one console row reading "57s over 7 frames", plate decided 4
    votes to 1, and the crop beside it taken from the frame that lost."""
    from ml.analytics.tracks import _split_on_gaps
    segments = _split_on_gaps([_at(0), _at(0.5), _at(1.0), _at(1.5), _at(57.0), _at(57.2)])
    assert [[r["mark"] for r in s] for s in segments] == [[0, 0.5, 1.0, 1.5], [57.0, 57.2]]


def test_ordinary_occlusion_is_not_a_split():
    """A vehicle behind a bus for three seconds is still the same vehicle. 91% of
    this estate's frame gaps are under 3s, which is cadence plus occlusion."""
    from ml.analytics.tracks import _split_on_gaps
    assert len(_split_on_gaps([_at(0), _at(3), _at(6), _at(9)])) == 1


def test_a_continuous_pass_is_left_alone():
    from ml.analytics.tracks import _split_on_gaps
    assert len(_split_on_gaps([_at(i * 0.5) for i in range(20)])) == 1


def test_splitter_handles_the_degenerate_inputs():
    from ml.analytics.tracks import _split_on_gaps
    assert _split_on_gaps([]) == []
    assert len(_split_on_gaps([_at(0)])) == 1
