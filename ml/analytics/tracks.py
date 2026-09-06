"""Fold frame-level ``detections`` into track-level ``tracks``.

Why this exists
---------------
``ml/pipeline.py`` writes one row per box per processed frame. That is the right
shape for inference and the wrong shape for every question an operator asks. "Is
this the same motorcycle that came through Paldi eight minutes ago" is a track
question. "What is the plate" is a track question — no single frame answers it
reliably (see :mod:`ml.analytics.anpr`). "Did it loiter" is a track question by
definition.

So this module is the fold: group detections by ``(camera_id, track_id,
session_key)``, and for each group derive the things a frame cannot know —
duration, path, net heading, a voted class and colour, a consensus plate, and a
mean embedding.

Identity caveat
---------------
``track_id`` comes from ByteTrack and is unique only *within one run of one
camera*. Two runs both start numbering at 1. ``session_key`` is what keeps them
apart; it is part of the group key here and part of the UNIQUE constraint on
``tracks``. Detections with ``track_id IS NULL`` (the tracker declined to
associate them) cannot be folded and are left alone — they remain searchable as
individual detections.

Idempotency
-----------
Upsert on ``(camera_id, track_id, session_key)``. Re-running over the same
window updates rows in place rather than duplicating them, so this is safe to
run on a cron, safe to re-run after a crash, and safe to run while the live
worker is still appending to a track that has not ended yet.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from typing import Any, Iterable, Optional

from ml.analytics.anpr import consensus_plate
from ml.analytics.geometry import (
    bbox_diag,
    bbox_ground_point,
    heading_deg,
    normalise,
)

log = logging.getLogger(__name__)


def vector_to_list(value: Any) -> list[float]:
    """Coerce whatever a ``vector`` column came back as into a list of floats.

    psycopg2 hands back a different Python type depending on the pgvector
    version: 0.5.x returns a ``pgvector.Vector`` (not iterable), earlier versions
    returned a numpy array, and with no typecaster registered you get the raw
    text ``'[0.1,0.2,…]'``. Assuming any one of them breaks on the others, and
    the failure is a ``TypeError`` deep inside an aggregate read, so it is worth
    one small function rather than a cast at each call site.
    """
    if value is None:
        return []
    to_list = getattr(value, "to_list", None)      # pgvector.Vector (0.5+)
    if callable(to_list):
        return [float(v) for v in to_list()]
    tolist = getattr(value, "tolist", None)        # numpy ndarray
    if callable(tolist):
        return [float(v) for v in tolist()]
    if isinstance(value, str):                     # no typecaster registered
        return [float(v) for v in value.strip().strip("[]").split(",") if v.strip()]
    return [float(v) for v in value]


# A track's stored path is for drawing and for the spread/heading maths, not for
# forensics — the detections themselves are the record. 120 points is enough to
# render a smooth polyline at any sane zoom and keeps the JSONB small enough
# that selecting 500 tracks is still one fast query.
MAX_PATH_POINTS = 120

# Below this many frames a "track" is usually a detector flicker: one or two
# boxes that never associated into anything. They are still written (an operator
# searching for a plate should find them) but they are marked so rules can skip
# them — a two-frame track has no meaningful duration or heading.
MIN_MEANINGFUL_FRAMES = 3

# Fallback frame sizes, tried largest-first, when a detection predates the
# frame_w/frame_h columns. See _infer_frame_size.
_COMMON_RESOLUTIONS = [
    (1920, 1080), (1600, 900), (1280, 720), (1024, 576), (854, 480), (640, 360),
]


# ── frame size ───────────────────────────────────────────────────────────────

def _infer_frame_size(conn, camera_id: str) -> tuple[int, int]:
    """Best-effort frame size for a camera whose detections lack frame_w/h.

    ``detections.frame_w``/``frame_h`` were added late; rows written before that
    have NULL. Normalising a path or testing a zone polygon needs the frame size,
    so we infer it from the largest bbox extent ever seen on that camera and snap
    up to the nearest common resolution.

    This is a fallback, not a substitute. It is wrong whenever nothing on that
    camera ever came near the frame edge, which is exactly why the pipeline now
    records the real value. Anything written by the current pipeline takes the
    recorded value and never reaches this function.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max((bbox->>'x2')::float) AS mx, max((bbox->>'y2')::float) AS my
            FROM detections WHERE camera_id = %s
            """,
            (camera_id,),
        )
        row = cur.fetchone() or {}
    max_x = float(row.get("mx") or 0.0)
    max_y = float(row.get("my") or 0.0)
    if max_x <= 0 or max_y <= 0:
        return (1920, 1080)
    # Smallest common resolution that still contains every box seen.
    fitting = [(w, h) for w, h in _COMMON_RESOLUTIONS if max_x <= w and max_y <= h]
    if fitting:
        return min(fitting, key=lambda wh: wh[0] * wh[1])
    # Larger than anything in the table (4K feed, or an odd aspect) — use the
    # observed extent directly.
    return (int(math.ceil(max_x)), int(math.ceil(max_y)))


# ── vote helpers ─────────────────────────────────────────────────────────────

def _weighted_vote(pairs: Iterable[tuple[Optional[str], float]]) -> tuple[Optional[str], dict]:
    """Confidence-weighted plurality vote.

    Weighting by detection confidence rather than counting raw frames matters:
    a vehicle entering frame at 0.42 confidence for six frames and sitting
    mid-frame at 0.93 for four should be classified on the four clear frames,
    not the six marginal ones. Returns ``(winner, tally)`` where the tally is
    stored as JSONB so the UI can show *how* close the call was.
    """
    tally: dict[str, float] = defaultdict(float)
    for value, weight in pairs:
        if not value:
            continue
        tally[value] += max(0.0, float(weight or 0.0))
    if not tally:
        return None, {}
    winner = max(tally, key=lambda k: tally[k])
    return winner, {k: round(v, 4) for k, v in sorted(tally.items(), key=lambda kv: -kv[1])}


def _downsample(points: list[dict], limit: int = MAX_PATH_POINTS) -> list[dict]:
    """Uniformly thin a path to ``limit`` points, always keeping both ends."""
    n = len(points)
    if n <= limit:
        return points
    step = (n - 1) / (limit - 1)
    idx = sorted({int(round(i * step)) for i in range(limit)} | {0, n - 1})
    return [points[i] for i in idx]


def _l2_normalise(vec: list[float]) -> list[float]:
    """Renormalise a mean-of-unit-vectors back onto the unit sphere.

    The member embeddings are each L2-normalised, but their mean is not — it is
    shorter the more the members disagree. pgvector's ``<=>`` is cosine distance
    so magnitude is divided out anyway, but the HNSW index and any dot-product
    scoring downstream assume unit length, so we restore it here.
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-9:
        return vec
    return [v / norm for v in vec]


# ── the fold ─────────────────────────────────────────────────────────────────

_DETECTION_COLS = """
    d.id, d.camera_id, d.track_id, d.session_key, d.class, d.confidence,
    d.dominant_color, d.plate_text, d.plate_confidence, d.bbox, d.ts, d.pts_ms,
    d.thumbnail_key, d.frame_w, d.frame_h, d.source
"""


def _fetch_detections(
    conn,
    camera_id: Optional[str] = None,
    since=None,
    session_key: Optional[str] = None,
    limit_tracks: Optional[int] = None,
) -> dict[tuple, list[dict]]:
    """Load the light columns of every trackable detection, grouped.

    Deliberately does **not** select ``embedding``: 512 floats × tens of
    thousands of rows is hundreds of megabytes over the wire to compute a mean
    the database can compute itself. The mean embedding is fetched per group in
    :func:`_fetch_mean_embeddings` with pgvector's ``avg()``.
    """
    clauses = ["d.track_id IS NOT NULL"]
    params: list[Any] = []
    if camera_id:
        clauses.append("d.camera_id = %s")
        params.append(camera_id)
    if since is not None:
        clauses.append("d.ts >= %s")
        params.append(since)
    if session_key:
        clauses.append("COALESCE(d.session_key, 'live') = %s")
        params.append(session_key)

    sql = f"""
        SELECT {_DETECTION_COLS}
        FROM detections d
        WHERE {' AND '.join(clauses)}
        ORDER BY d.camera_id, COALESCE(d.session_key, 'live'), d.track_id,
                 d.ts ASC, d.pts_ms ASC NULLS FIRST
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur:
            key = (str(row["camera_id"]), int(row["track_id"]),
                   row["session_key"] or "live")
            groups[key].append(dict(row))
    if limit_tracks is not None and len(groups) > limit_tracks:
        keep = list(groups)[:limit_tracks]
        groups = {k: groups[k] for k in keep}
    return groups


def _fetch_mean_embeddings(conn, keys: list[tuple]) -> dict[tuple, list[float]]:
    """One query for the mean embedding of every group, computed in Postgres.

    pgvector 0.5+ implements ``avg(vector)``, so the 512-dimensional mean never
    leaves the server as individual rows. Verified against the deployed
    extension before relying on it.
    """
    if not keys:
        return {}
    cams = list({k[0] for k in keys})
    sql = """
        SELECT camera_id, track_id, COALESCE(session_key, 'live') AS session_key,
               avg(embedding) AS mean_embedding
        FROM detections
        WHERE camera_id = ANY(%s::uuid[]) AND track_id IS NOT NULL
              AND embedding IS NOT NULL
        GROUP BY camera_id, track_id, COALESCE(session_key, 'live')
    """
    wanted = set(keys)
    out: dict[tuple, list[float]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, (cams,))
        for row in cur:
            key = (str(row["camera_id"]), int(row["track_id"]), row["session_key"])
            if key not in wanted or row["mean_embedding"] is None:
                continue
            out[key] = _l2_normalise(vector_to_list(row["mean_embedding"]))
    return out


def summarise_track(rows: list[dict], frame_size: tuple[int, int]) -> dict:
    """Derive one ``tracks`` row from that track's detections, in time order.

    ``rows`` must be sorted ascending by ``ts`` then ``pts_ms``. ``frame_size``
    is ``(w, h)`` — taken from the detections themselves when recorded, inferred
    per camera otherwise.
    """
    fw, fh = frame_size
    first, last = rows[0], rows[-1]

    cls, class_votes = _weighted_vote((r["class"], r["confidence"]) for r in rows)
    color, color_votes = _weighted_vote(
        (r["dominant_color"], r["confidence"]) for r in rows
    )

    # ── multi-frame plate consensus ───────────────────────────────────────────
    # Every read this track produced, including the ones that are individually
    # wrong. That is the point: the errors are decorrelated across frames, so
    # eleven bad reads out-vote the single best one.
    plate_reads = [
        (r["plate_text"], r["plate_confidence"])
        for r in rows
        if r["plate_text"]
    ]
    plate, plate_conf, plate_n, plate_votes = consensus_plate(plate_reads)

    # ── path in normalised coordinates ────────────────────────────────────────
    path: list[dict] = []
    for r in rows:
        bbox = r["bbox"] if isinstance(r["bbox"], dict) else json.loads(r["bbox"])
        gx, gy = normalise(bbox_ground_point(bbox), fw, fh)
        # Normalised box size travels with the point. The rule engine needs it:
        # accident detection compares box overlap between two tracks, and crowd
        # density needs occupied area — neither can be recovered from a centroid,
        # and neither should have to re-read every detection row to get it.
        path.append({
            "x": round(gx, 5),
            "y": round(gy, 5),
            "w": round(abs(bbox["x2"] - bbox["x1"]) / fw, 5),
            "h": round(abs(bbox["y2"] - bbox["y1"]) / fh, 5),
            "t": r["ts"].isoformat() if r["ts"] else None,
            "pts_ms": round(float(r["pts_ms"]), 1) if r["pts_ms"] is not None else None,
            "d": str(r["id"]),
        })

    # Net heading over the whole track, not frame-to-frame. Frame-to-frame
    # heading on a 6 FPS feed is mostly tracker jitter; the first→last vector is
    # what "which way was it going" actually means, and it is what the wrong-way
    # rule compares against a zone's permitted direction.
    hdg = heading_deg((path[0]["x"], path[0]["y"]), (path[-1]["x"], path[-1]["y"])) \
        if len(path) >= 2 else None
    dx = path[-1]["x"] - path[0]["x"]
    dy = path[-1]["y"] - path[0]["y"]
    displacement = math.sqrt(dx * dx + dy * dy)

    # Bounding radius of the path about its own mean. This is the loitering
    # discriminator: a vehicle queuing at a light has small displacement *and*
    # small spread; a vehicle circling a block has small displacement and large
    # spread. Displacement alone cannot tell those apart.
    cx = sum(p["x"] for p in path) / len(path)
    cy = sum(p["y"] for p in path) / len(path)
    spread = max(
        math.sqrt((p["x"] - cx) ** 2 + (p["y"] - cy) ** 2) for p in path
    ) if path else 0.0

    # ── duration ──────────────────────────────────────────────────────────────
    # pts_ms is the honest clock for archive footage: `ts` is wall-clock at the
    # moment of *inference*, which for a batch pass compresses 12 hours of video
    # into 20 minutes of processing. Where both exist, pts wins.
    dur = 0.0
    if first["pts_ms"] is not None and last["pts_ms"] is not None:
        dur = max(0.0, (float(last["pts_ms"]) - float(first["pts_ms"])) / 1000.0)
    if dur <= 0 and first["ts"] and last["ts"]:
        dur = max(0.0, (last["ts"] - first["ts"]).total_seconds())

    best = max(rows, key=lambda r: float(r["confidence"] or 0.0))

    return {
        "class": cls or first["class"],
        "class_votes": class_votes,
        "dominant_color": color,
        "color_votes": color_votes,
        "plate_text": plate,
        "plate_confidence": plate_conf if plate else None,
        "plate_read_count": plate_n,
        "plate_votes": plate_votes,
        "first_seen": first["ts"],
        "last_seen": last["ts"],
        "first_pts_ms": first["pts_ms"],
        "last_pts_ms": last["pts_ms"],
        "duration_s": round(dur, 3),
        "detection_count": len(rows),
        "best_detection_id": str(best["id"]),
        "thumbnail_key": best.get("thumbnail_key"),
        "path": _downsample(path),
        "heading_deg": round(hdg, 2) if hdg is not None else None,
        "displacement_norm": round(displacement, 5),
        "max_spread_norm": round(spread, 5),
        "source": first.get("source") or "live",
        "frame_w": fw,
        "frame_h": fh,
        "detection_ids": [str(r["id"]) for r in rows],
    }


_UPSERT = """
INSERT INTO tracks (
    camera_id, track_id, session_key, class, class_votes, dominant_color,
    color_votes, plate_text, plate_confidence, plate_read_count, plate_votes,
    first_seen, last_seen, first_pts_ms, last_pts_ms, duration_s,
    detection_count, best_detection_id, thumbnail_key, path, heading_deg,
    displacement_norm, max_spread_norm, embedding, source
) VALUES (
    %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s::jsonb,
    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s
)
ON CONFLICT (camera_id, track_id, session_key) DO UPDATE SET
    class             = EXCLUDED.class,
    class_votes       = EXCLUDED.class_votes,
    dominant_color    = EXCLUDED.dominant_color,
    color_votes       = EXCLUDED.color_votes,
    plate_text        = EXCLUDED.plate_text,
    plate_confidence  = EXCLUDED.plate_confidence,
    plate_read_count  = EXCLUDED.plate_read_count,
    plate_votes       = EXCLUDED.plate_votes,
    first_seen        = LEAST(tracks.first_seen, EXCLUDED.first_seen),
    last_seen         = GREATEST(tracks.last_seen, EXCLUDED.last_seen),
    first_pts_ms      = EXCLUDED.first_pts_ms,
    last_pts_ms       = EXCLUDED.last_pts_ms,
    duration_s        = EXCLUDED.duration_s,
    detection_count   = EXCLUDED.detection_count,
    best_detection_id = EXCLUDED.best_detection_id,
    thumbnail_key     = EXCLUDED.thumbnail_key,
    path              = EXCLUDED.path,
    heading_deg       = EXCLUDED.heading_deg,
    displacement_norm = EXCLUDED.displacement_norm,
    max_spread_norm   = EXCLUDED.max_spread_norm,
    embedding         = EXCLUDED.embedding,
    source            = EXCLUDED.source
RETURNING id
"""


def _upsert_track(conn, key: tuple, summary: dict, embedding: Optional[list[float]]) -> str:
    camera_id, track_id, session_key = key
    with conn.cursor() as cur:
        cur.execute(_UPSERT, (
            camera_id, track_id, session_key,
            summary["class"], json.dumps(summary["class_votes"]),
            summary["dominant_color"], json.dumps(summary["color_votes"]),
            summary["plate_text"], summary["plate_confidence"],
            summary["plate_read_count"], json.dumps(summary["plate_votes"]),
            summary["first_seen"], summary["last_seen"],
            summary["first_pts_ms"], summary["last_pts_ms"],
            summary["duration_s"], summary["detection_count"],
            summary["best_detection_id"], summary["thumbnail_key"],
            json.dumps(summary["path"]), summary["heading_deg"],
            summary["displacement_norm"], summary["max_spread_norm"],
            embedding, summary["source"],
        ))
        return str(cur.fetchone()["id"])


def _link_detections(conn, track_uuid: str, detection_ids: list[str]) -> None:
    """Point every member detection back at its track.

    Kept as an explicit FK rather than relying on the (camera, track_id, session)
    tuple: it makes "show me every frame of this track" a single indexed lookup,
    and it means a track row can be deleted without orphaning its detections.
    """
    if not detection_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE detections SET track_uuid = %s WHERE id = ANY(%s::uuid[])",
            (track_uuid, detection_ids),
        )


def build_tracks(
    conn,
    camera_id: Optional[str] = None,
    since=None,
    session_key: Optional[str] = None,
    limit_tracks: Optional[int] = None,
    commit_every: int = 200,
) -> dict:
    """Fold detections into tracks. Returns a summary dict for the caller to log.

    Args:
        camera_id: restrict to one camera (UUID). None = all cameras.
        since: only fold detections with ``ts >= since``. Note this is a
            *detection* filter, so a track straddling the boundary is rebuilt
            from its visible part only — pass a boundary comfortably before the
            oldest track you care about, or omit it for a full rebuild.
        session_key: restrict to one run's tracks.
        limit_tracks: cap the number of groups processed (useful for smoke tests).
        commit_every: commit after this many tracks so a long run is resumable
            and does not hold one enormous transaction open.
    """
    groups = _fetch_detections(conn, camera_id, since, session_key, limit_tracks)
    if not groups:
        return {"tracks": 0, "detections": 0, "skipped_short": 0, "plates": 0}

    means = _fetch_mean_embeddings(conn, list(groups))

    # Frame size: prefer what the pipeline recorded, fall back to per-camera
    # inference (memoised — the fallback query is a full scan of that camera).
    inferred: dict[str, tuple[int, int]] = {}

    written = detections_seen = short = plates = 0
    for key, rows in groups.items():
        cam = key[0]
        fw = next((r["frame_w"] for r in rows if r.get("frame_w")), None)
        fh = next((r["frame_h"] for r in rows if r.get("frame_h")), None)
        if not (fw and fh):
            if cam not in inferred:
                inferred[cam] = _infer_frame_size(conn, cam)
                log.info("camera %s: frame size inferred as %s (no frame_w/h on "
                         "its detections)", cam, inferred[cam])
            fw, fh = inferred[cam]

        summary = summarise_track(rows, (int(fw), int(fh)))
        if summary["detection_count"] < MIN_MEANINGFUL_FRAMES:
            short += 1
        track_uuid = _upsert_track(conn, key, summary, means.get(key))
        _link_detections(conn, track_uuid, summary["detection_ids"])

        written += 1
        detections_seen += summary["detection_count"]
        if summary["plate_text"]:
            plates += 1
        if written % commit_every == 0:
            conn.commit()

    conn.commit()
    return {
        "tracks": written,
        "detections": detections_seen,
        "skipped_short": short,
        "plates": plates,
    }
