"""Cross-camera subject reconstruction, scored as evidence rather than asserted as fact.

The question this answers
------------------------
An operator has one sighting — a vehicle at one camera at one time — and needs to
know where it went. Doing that by hand means opening every neighbouring camera,
guessing when to look, and scrubbing. This does the guessing, and produces a ranked
list of candidate sightings with the reasoning attached.

Why it is built as a likelihood ratio and not a match score
-----------------------------------------------------------
A cosine similarity of 0.88 between two vehicle crops means nothing on its own.
``ml/watchlist.py`` measured CLIP at 0.76–0.91 between *completely different*
vehicles, so a system that thresholds on similarity will confidently report matches
all night. The number only becomes information when compared against a reference:
how unusual is 0.88 *at this camera, among vehicles of this class*? At a camera
whose whole field of view is white hatchbacks the answer is "not at all".

So every factor here is expressed as a likelihood ratio — how much more likely the
observation is if this candidate is the subject than if it is not — and the ratios
multiply into a posterior. That framing buys three things a match score cannot:

1. **Evidence against is representable.** Two confident plate reads that disagree
   push the score *down*. A weighted-sum scorer can only ever add.
2. **The size of the search is accounted for.** Finding a good visual match among
   200 candidates is much weaker evidence than finding it among 3. The prior is
   divided by the number of same-class vehicles in the window, which is the
   database-search correction most systems silently omit.
3. **Every number is inspectable.** ``score_breakdown`` carries each ratio, and each
   one is labelled ``empirical`` (measured from this data) or ``assumed`` (a stated
   modelling choice). An operator can see that a match rests on a plate rather than
   on a colour coincidence — and a court can see which parts were assumed.

What it never does
------------------
It never writes a confirmed sighting. ``pursuit_sightings.confirmed`` stays NULL
until a person looks. The engine's output is a shortlist and an argument; the
identification is the operator's.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from backend.app import db
from backend.app.config import settings
from backend.app.services import graph

log = logging.getLogger("setu.api.dragnet")


# ── Plate comparison ───────────────────────────────────────────────────────
# ``ml.analytics.anpr.plate_similarity`` already implements confusion-aware plate
# comparison — a substitution inside a glyph class (O/0, I/1, S/5, B/8, Z/2, G/6)
# costs a quarter of a real one — and its docstring names this engine as the
# consumer. Imported rather than reimplemented: two confusion tables in one repo
# would drift, and the one in ``ml`` is the one ``scripts/score_anpr.py`` measures
# against, which is where the thresholds below get their calibration.
#
# On that scale, measured across 3,188 tracks of known ground truth:
#
#   glyph confusions only     0.925 – 0.977   (misread of the same plate)
#   one genuine character     0.889 – 0.909   (a different vehicle)
#
# Hence a floor of 0.92 rather than the 0.45 that suits ``search.py``: there, one
# side is a fragment an operator typed; here both sides are full machine reads, and
# a single genuine character difference between two Indian registrations means two
# different vehicles. The gap is narrow and length-dependent — the numbers above
# are for 9–11 character plates, which is the whole domain — so the floor is set
# just above the worst case rather than in the middle.
#
# One known limitation, accepted deliberately: the shared function charges a full
# edit for a *dropped* character, so a read that lost a digit to a bracket scores
# 0.900 — the same as a genuine difference — and lands in the conflict band below.
# That is the safe direction of error for this engine. A false link between two
# vehicles is far more damaging in an investigation than a lead it failed to raise,
# and the visual and corridor evidence can still carry a hop the plate could not.
from ml.analytics.anpr import plate_similarity


# ── The anchor: what we are looking for ─────────────────────────────────────

@dataclass
class Anchor:
    """The subject, as the system currently understands it.

    Built from a ``tracks`` row when possible. A track is the consensus of every
    frame the vehicle appeared in — its plate is 85.2% exact against 17.6% for a
    single read, and its embedding is the renormalised mean of the member crops
    rather than one possibly-motion-blurred frame. Seeding a reconstruction from a
    single detection is supported, and is measurably worse.
    """

    camera_id: str
    ts: datetime
    vclass: str
    kind: str = "track"                 # track | detection
    track_uuid: Optional[str] = None
    detection_id: Optional[str] = None
    color: Optional[str] = None
    plate: Optional[str] = None
    plate_confidence: float = 0.0
    embedding: list[float] = field(default_factory=list)
    thumbnail_key: Optional[str] = None
    camera_name: Optional[str] = None

    @property
    def has_embedding(self) -> bool:
        return bool(self.embedding)


async def load_anchor(
    track_uuid: Optional[str] = None, detection_id: Optional[str] = None
) -> Optional[Anchor]:
    if track_uuid:
        row = await db.fetchrow(
            """
            SELECT t.id, t.camera_id, t.class, t.dominant_color, t.plate_text,
                   t.plate_confidence, t.first_seen, t.thumbnail_key, t.best_detection_id,
                   t.embedding, c.name AS camera_name
            FROM tracks t JOIN cameras c ON c.id = t.camera_id WHERE t.id = $1
            """,
            track_uuid,
        )
        if row is None:
            return None
        return Anchor(
            kind="track", track_uuid=str(row["id"]), detection_id=(
                str(row["best_detection_id"]) if row["best_detection_id"] else None
            ),
            camera_id=str(row["camera_id"]), ts=row["first_seen"], vclass=row["class"],
            color=row["dominant_color"], plate=row["plate_text"],
            plate_confidence=float(row["plate_confidence"] or 0.0),
            embedding=db.vector_to_list(row["embedding"]),
            thumbnail_key=row["thumbnail_key"], camera_name=row["camera_name"],
        )

    if detection_id:
        row = await db.fetchrow(
            """
            SELECT d.id, d.camera_id, d.class, d.dominant_color, d.plate_text,
                   d.plate_confidence, d.ts, d.thumbnail_key, d.track_uuid, d.embedding,
                   c.name AS camera_name
            FROM detections d JOIN cameras c ON c.id = d.camera_id WHERE d.id = $1
            """,
            detection_id,
        )
        if row is None:
            return None
        # A detection that belongs to a track is upgraded to its track: the operator
        # clicked one frame, but the consensus over every frame of that vehicle is
        # strictly better evidence and costs one extra query to reach.
        if row["track_uuid"]:
            upgraded = await load_anchor(track_uuid=str(row["track_uuid"]))
            if upgraded is not None:
                upgraded.detection_id = str(row["id"])
                return upgraded
        return Anchor(
            kind="detection", detection_id=str(row["id"]),
            camera_id=str(row["camera_id"]), ts=row["ts"], vclass=row["class"],
            color=row["dominant_color"], plate=row["plate_text"],
            plate_confidence=float(row["plate_confidence"] or 0.0),
            embedding=db.vector_to_list(row["embedding"]),
            thumbnail_key=row["thumbnail_key"], camera_name=row["camera_name"],
        )
    return None


# ── Per-camera visual calibration ───────────────────────────────────────────

@dataclass
class Background:
    """The null distribution: what a *stranger* scores against this anchor here.

    Sampled per camera and per class, because that is the population a false match
    would be drawn from. A camera pointed down a narrow lane at 30° sees vehicles in
    near-identical pose against an identical backdrop; CLIP cosines there run high
    for everything, and a threshold calibrated at a wide-angle junction would fire on
    every car that passes. This is the only way the visual number becomes comparable
    across cameras.
    """

    scope: str                       # camera | district | global | none
    n: int
    mean: float = 0.0
    std: float = 0.0
    cosines: list[float] = field(default_factory=list)   # sorted ascending

    def tail(self, cos: float) -> dict:
        """How often a stranger scores at least this well.

        Empirical, from the sample — no distribution is assumed. When the candidate
        beats every sample the tail is reported as ``1/(n+1)`` rather than zero: forty
        strangers cannot evidence a one-in-a-thousand coincidence, and a zero here
        would produce an infinite likelihood ratio from a small sample. The bound is
        the honest ceiling on what this camera's history can support, and it is
        labelled so the operator can see the claim was capped.
        """
        if self.n == 0:
            return {"p_chance": 1.0, "method": "none", "z": None, "beat": 0}
        beat = sum(1 for c in self.cosines if c >= cos)
        floor = 1.0 / (self.n + 1)
        p = beat / self.n
        z = (cos - self.mean) / self.std if self.std else None
        return {
            "p_chance": max(p, floor),
            "method": "empirical" if p > 0 else "sample_bounded",
            "z": round(z, 2) if z is not None else None,
            "beat": beat,
        }


_BACKGROUND_MIN = 25

# Floor for a physically impossible arrival time. Small enough to bury a candidate
# beneath anything plausible, large enough that its other factors are still legible in
# the breakdown — 1e-4 turns a 4260x plate match into 0.43, i.e. slightly against.
_IMPOSSIBLE_TIMING_FLOOR = 1e-4


async def background_for(anchor: Anchor, camera_id: str) -> Background:
    """Sample same-class cosines at this camera, widening the scope if too few.

    Widening is a compromise and is reported as one. A district-wide background mixes
    camera geometries and so understates how unusual a high cosine is at a tight
    camera; it is still far better than an absolute threshold, and ``scope`` tells the
    operator which they are looking at.
    """
    if not anchor.has_embedding:
        return Background(scope="none", n=0)

    literal = db.to_vector_literal(anchor.embedding)
    exclude = anchor.track_uuid or "00000000-0000-0000-0000-000000000000"

    # ORDER BY random() is a sequential scan of the per-camera slice. At ~150 tracks
    # per camera that is cheaper than the alternative it replaces (transferring the
    # vectors here and sampling in Python), and it keeps the sample unbiased by
    # physical row order.
    async def sample(where: str, *args) -> list[float]:
        rows = await db.fetch(
            f"""
            SELECT 1 - (t.embedding <=> $1::vector) AS cos
            FROM tracks t
            JOIN cameras c ON c.id = t.camera_id
            WHERE t.embedding IS NOT NULL AND t.class = $2 AND t.id <> $3::uuid
              AND {where}
            ORDER BY random() LIMIT {int(settings.dragnet_background_sample)}
            """,
            literal, anchor.vclass, exclude, *args,
        )
        return [float(r["cos"]) for r in rows]

    scopes = [
        ("camera", "t.camera_id = $4::uuid", (camera_id,)),
        ("district", "c.district = (SELECT district FROM cameras WHERE id = $4::uuid)", (camera_id,)),
        ("global", "TRUE", ()),
    ]
    for scope, where, args in scopes:
        cosines = await sample(where, *args)
        if len(cosines) >= _BACKGROUND_MIN or scope == "global":
            if not cosines:
                return Background(scope="none", n=0)
            cosines.sort()
            mean = sum(cosines) / len(cosines)
            var = sum((c - mean) ** 2 for c in cosines) / max(len(cosines) - 1, 1)
            return Background(scope=scope, n=len(cosines), mean=mean,
                              std=math.sqrt(var), cosines=cosines)
    return Background(scope="none", n=0)


# ── Evidence constants, with their provenance ───────────────────────────────
# Measured on this project's own data by ``scripts/score_anpr.py`` over 3,188 tracks
# whose intended plate is known because the seeder generated it:
_P_PLATE_EXACT_GIVEN_SAME = 0.852       # consensus exact accuracy
_P_PLATE_DISAGREE_GIVEN_SAME = 0.148    # its complement
# Below 0.40 reported confidence, consensus accuracy is 62.9%; from 0.40 to 0.60 it
# is 95.4%. So 0.40 is where a plate read becomes trustworthy enough that its
# *disagreement* with another read means something rather than nothing.
_MIN_PLATE_CONF_FOR_CONFLICT = 0.40
# A single factor should not be able to swamp every other piece of evidence, however
# well measured. 5,000 is roughly the plate diversity this deployment has observed,
# which is the point past which "no other vehicle has this plate" stops being
# supported by anything we can see.
_PLATE_LR_CAP = 5000.0

# Assumed, not measured — labelled as such in every breakdown this produces.
# Colour agreement is imperfect under sodium lighting and at dusk; a true match
# agreeing three times in four is a deliberately unambitious estimate.
_P_COLOR_AGREE_GIVEN_SAME = 0.75
# The probability a genuine re-sighting lands in the top tail of its own background.
# It cannot be measured without labelled cross-camera pairs, which this deployment
# does not have. Held low enough to be conservative: raising it inflates every visual
# ratio uniformly, which would change rankings not at all and confidence wrongly.
_P_VISUAL_TOP_GIVEN_SAME = 0.70


def _time_lr(transit_s: float, p10: float, med: float, p90: float, horizon_s: float) -> dict:
    """How much the arrival time favours this candidate over an arbitrary one.

    A lognormal on transit time — journey times are positively skewed and cannot be
    negative, which rules out a Gaussian — against the alternative that the arrival
    time is uniform across the searched window. Both are densities per second, so the
    ratio is dimensionless and can be multiplied with the others.

    This factor is what makes an implausibly-timed match *lose*. A vehicle appearing
    at a camera 4 km away 20 seconds later is not the same vehicle, and a scorer built
    from similarity alone has no way to say so.
    """
    if med is None or med <= 0 or horizon_s <= 0:
        # Genuinely no information: the corridor has no transit statistics to compare
        # against. Neutral is the honest answer — this factor abstains and the others
        # decide.
        return {"lr": 1.0, "basis": "unusable", "expected_s": med, "observed_s": transit_s}

    lo = p10 if p10 and p10 > 0 else med * 0.5
    hi = p90 if p90 and p90 > 0 else med * 2.0

    if transit_s <= 0:
        # An arrival at or before the departure it supposedly follows. This is *not*
        # missing data, and returning 1.0 here was the single worst failure in this
        # scorer: the one factor whose job is to reject implausible timing abstained on
        # the most implausible timing there is, and a same-plate track from an earlier
        # leg of the vehicle's own journey came back as a forward arrival with a
        # posterior of 0.999997.
        #
        # Not a hard zero, though a lognormal's density there really is zero. Model 2
        # ingests from separate departmental systems whose clocks are synchronised by
        # nobody in particular, and the anchor's timestamp is a track's first_seen
        # while a candidate at an overlapping camera may legitimately enter frame a few
        # seconds earlier. So the penalty scales with how impossible the deficit is,
        # measured in units of the corridor's own fastest plausible transit, and floors
        # rather than annihilating — a buried candidate whose plate still reads 4260x
        # can be inspected, a candidate multiplied by zero cannot.
        deficit = -transit_s
        lr = max(math.exp(-1.0 - deficit / max(lo, 1.0)), _IMPOSSIBLE_TIMING_FLOOR)
        return {
            "lr": lr, "basis": "arrival_before_departure",
            "expected_s": round(med, 1), "observed_s": round(transit_s, 1),
            "seconds_early": round(deficit, 1),
            "note": ("observed at this camera before the subject left the previous one — "
                     "evidence against this hop, not evidence for an earlier one. Where "
                     "the subject came *from* is a chronological question: see "
                     "/investigate/plate/{plate}."),
        }
    # 10th-to-90th percentile spans 2.5631 standard deviations of a normal.
    sigma = max((math.log(hi) - math.log(lo)) / 2.5631, 0.15)
    mu = math.log(med)
    z = (math.log(transit_s) - mu) / sigma
    density = math.exp(-0.5 * z * z) / (transit_s * sigma * math.sqrt(2 * math.pi))
    return {
        "lr": density * horizon_s,
        "basis": "lognormal_vs_uniform",
        "expected_s": round(med, 1), "observed_s": round(transit_s, 1),
        "sigma_log": round(sigma, 3), "z": round(z, 2),
    }


def _plate_lr(anchor: Anchor, cand_plate: Optional[str], cand_conf: float) -> dict:
    """Plate evidence, including the case where it argues *against*."""
    sim = plate_similarity(anchor.plate, cand_plate)
    if not anchor.plate or not cand_plate:
        return {"lr": 1.0, "verdict": "absent", "similarity": None,
                "note": "no plate on one side — evidence about camera angle, not identity"}

    exact_lr = min(_P_PLATE_EXACT_GIVEN_SAME * _PLATE_LR_CAP, _PLATE_LR_CAP)
    floor = settings.dragnet_plate_similarity_floor

    if sim >= 0.999:
        return {"lr": exact_lr, "verdict": "exact", "similarity": 1.0,
                "calibration": "empirical (85.2% consensus exact accuracy)"}
    if sim >= floor:
        # Interpolate in log space from no evidence at the floor to full exact-match
        # evidence at identity. Linear interpolation of the ratio itself would give a
        # near-miss almost the weight of an exact match.
        fraction = (sim - floor) / (1.0 - floor)
        return {"lr": math.exp(fraction * math.log(exact_lr)), "verdict": "confusion_match",
                "similarity": round(sim, 3), "floor": floor,
                "calibration": "empirical scale, interpolated"}

    if (anchor.plate_confidence >= _MIN_PLATE_CONF_FOR_CONFLICT
            and cand_conf >= _MIN_PLATE_CONF_FOR_CONFLICT):
        return {"lr": _P_PLATE_DISAGREE_GIVEN_SAME, "verdict": "conflict",
                "similarity": round(sim, 3),
                "note": "two confident reads that disagree — evidence against",
                "calibration": "empirical (14.8% of consensus reads are wrong)"}
    return {"lr": 0.6, "verdict": "weak_conflict", "similarity": round(sim, 3),
            "note": "reads disagree but at least one is below 0.40 confidence",
            "calibration": "assumed"}


def _color_lr(anchor: Anchor, cand_color: Optional[str], color_freq: float, pool_n: int) -> dict:
    """Colour evidence, weighed against how common that colour is here.

    A white vehicle matching a white vehicle at a camera where 60% of traffic is white
    is almost no evidence. The frequency is measured from the candidate pool itself
    rather than assumed, which is the only part of this factor that is not a guess.
    """
    if not anchor.color or not cand_color:
        return {"lr": 1.0, "verdict": "absent"}
    floor = 1.0 / (pool_n + 1)
    if anchor.color == cand_color:
        lr = _P_COLOR_AGREE_GIVEN_SAME / max(color_freq, floor)
        return {"lr": lr, "verdict": "match", "color": cand_color,
                "frequency_here": round(color_freq, 3), "calibration": "assumed × empirical"}
    lr = (1.0 - _P_COLOR_AGREE_GIVEN_SAME) / max(1.0 - color_freq, floor)
    return {"lr": lr, "verdict": "mismatch", "color": cand_color,
            "anchor_color": anchor.color, "frequency_here": round(color_freq, 3),
            "calibration": "assumed × empirical"}


def _visual_lr(cos: Optional[float], background: Background) -> dict:
    """The CLIP cosine, turned into evidence by comparison with strangers.

    The whole point of the engine in one function. A raw cosine of 0.88 is
    uninterpretable; ``p_chance`` — the share of same-class vehicles at this camera
    that score at least as well — is not. The ratio is how much more often a genuine
    re-sighting lands in that tail than a stranger does.
    """
    if cos is None or background.n == 0:
        return {"lr": 1.0, "verdict": "unavailable", "cosine": cos,
                "background_scope": background.scope, "background_n": background.n}
    t = background.tail(cos)
    lr = _P_VISUAL_TOP_GIVEN_SAME / t["p_chance"]
    return {
        "lr": lr,
        "verdict": "calibrated",
        "cosine": round(cos, 4),
        "p_chance": round(t["p_chance"], 5),
        "tail_method": t["method"],
        "beaten_by": t["beat"],
        "z": t["z"],
        "background_scope": background.scope,
        "background_n": background.n,
        "background_mean": round(background.mean, 4),
        "background_std": round(background.std, 4),
        "calibration": "empirical background, assumed 0.70 numerator",
        "note": (
            "candidate beat every sampled stranger; evidence capped at what a "
            f"{background.n}-sample background can support"
        ) if t["method"] == "sample_bounded" else None,
    }


def score_candidate(
    anchor: Anchor,
    cand: dict,
    edge: dict,
    background: Background,
    pool: dict,
) -> dict:
    """Combine the factors into a posterior, and show all the working.

    The prior is the part most systems skip. ``corridor_probability / pool_size`` says
    that before looking at any similarity, this candidate is one of N same-class
    vehicles at a camera the subject may not have visited at all. Skipping it is what
    lets a scorer report 94% confidence on the best of two hundred coincidences.
    """
    transit_s = (cand["first_seen"] - anchor.ts).total_seconds()
    corridor_p = float(edge.get("probability") or 0.0)
    pool_n = max(int(pool.get("n") or 1), 1)

    prior_p = min(max(corridor_p / pool_n, 1e-9), 0.5)
    prior_odds = prior_p / (1.0 - prior_p)

    time_f = _time_lr(transit_s, edge.get("p10_transit_s"), edge.get("median_transit_s"),
                      edge.get("p90_transit_s"), float(pool.get("horizon_s") or 1.0))
    plate_f = _plate_lr(anchor, cand.get("plate_text"), float(cand.get("plate_confidence") or 0.0))
    color_f = _color_lr(anchor, cand.get("dominant_color"),
                        float(pool.get("color_freq") or 0.0), pool_n)
    visual_f = _visual_lr(
        float(cand["visual_cos"]) if cand.get("visual_cos") is not None else None, background
    )

    evidence_ratio = time_f["lr"] * plate_f["lr"] * color_f["lr"] * visual_f["lr"]
    odds = prior_odds * evidence_ratio
    posterior = odds / (1.0 + odds)

    if plate_f["verdict"] == "exact":
        kind = "plate_exact"
    elif plate_f["verdict"] == "confusion_match":
        kind = "plate_fuzzy"
    else:
        kind = "visual"

    return {
        "score": round(posterior, 6),
        "evidence_ratio": round(evidence_ratio, 3),
        "match_kind": kind,
        "breakdown": {
            "posterior": round(posterior, 6),
            "evidence_ratio": round(evidence_ratio, 3),
            "prior": {
                "probability": round(prior_p, 8),
                "corridor_probability": round(corridor_p, 5),
                "corridor_evidence": edge.get("evidence"),
                "corridor_observations": edge.get("observations"),
                "candidate_pool": pool_n,
                "pool_truncated": bool(pool.get("truncated")),
                "hops": edge.get("hops", 1),
                "reference_population": (
                    f"{anchor.vclass} at this camera in the ETA window — class is the "
                    "reference set, not a scored factor"
                ),
            },
            "time": time_f,
            "plate": plate_f,
            "color": color_f,
            "visual": visual_f,
            "transit_s": round(transit_s, 1),
            "interpretation": (
                f"{round(evidence_ratio, 1)}× more consistent with the subject than with "
                f"a random {anchor.vclass} passing this camera in the window"
            ),
        },
    }


# ── One hop ─────────────────────────────────────────────────────────────────

async def candidates_at(
    anchor: Anchor,
    camera_id: str,
    eta_from: datetime,
    eta_to: datetime,
    exclude: Optional[list[str]] = None,
) -> tuple[list[dict], bool]:
    """Same-class tracks at one camera inside one ETA window.

    The cosine is computed **in Postgres** and only the scalar comes back. Pulling
    512-float vectors here to compare them in Python would move ~6 KB per candidate:
    a dozen cameras of 400 candidates is 30 MB per hop, for arithmetic the database
    can do against an HNSW index it already has.
    """
    literal = db.to_vector_literal(anchor.embedding) if anchor.has_embedding else None
    cos_expr = "1 - (t.embedding <=> $5::vector)" if literal else "NULL::double precision"
    args: list = [camera_id, anchor.vclass, eta_from, eta_to]
    if literal:
        args.append(literal)
    excl = [e for e in (exclude or []) if e]
    excl_sql = ""
    if excl:
        args.append(excl)
        excl_sql = f"AND t.id <> ALL(${len(args)}::uuid[])"

    limit = int(settings.dragnet_max_candidates_per_camera)
    rows = await db.fetch(
        f"""
        SELECT t.id AS track_uuid, t.camera_id, t.class, t.dominant_color, t.plate_text,
               t.plate_confidence, t.plate_read_count, t.first_seen, t.last_seen,
               t.duration_s, t.detection_count, t.thumbnail_key, t.best_detection_id,
               t.heading_deg, t.first_pts_ms,
               {cos_expr} AS visual_cos,
               c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng
        FROM tracks t JOIN cameras c ON c.id = t.camera_id
        WHERE t.camera_id = $1::uuid AND t.class = $2
          AND t.first_seen >= $3 AND t.first_seen <= $4
          {excl_sql}
        ORDER BY t.first_seen
        LIMIT {limit + 1}
        """,
        *args,
    )
    truncated = len(rows) > limit
    return rows[:limit], truncated


async def hop(
    anchor: Anchor,
    edge: dict,
    eta_from: datetime,
    eta_to: datetime,
    horizon_s: float,
    background: Optional[Background] = None,
    exclude: Optional[list[str]] = None,
) -> list[dict]:
    """Score every candidate at one camera. Returns them sorted, best first."""
    cands, truncated = await candidates_at(
        anchor, str(edge["camera_id"]), eta_from, eta_to, exclude
    )
    if not cands:
        return []

    if background is None:
        background = await background_for(anchor, str(edge["camera_id"]))

    same_color = sum(1 for c in cands if anchor.color and c["dominant_color"] == anchor.color)
    pool = {
        "n": len(cands),
        "truncated": truncated,
        "color_freq": same_color / len(cands) if cands else 0.0,
        "horizon_s": horizon_s,
    }

    scored: list[dict] = []
    for cand in cands:
        result = score_candidate(anchor, cand, edge, background, pool)
        row = {k: v for k, v in cand.items() if k != "visual_cos"}
        row.update({
            "visual_cos": cand["visual_cos"],
            "score": result["score"],
            "evidence_ratio": result["evidence_ratio"],
            "match_kind": result["match_kind"],
            "score_breakdown": result["breakdown"],
            "hops": edge.get("hops", 1),
            "via": edge.get("via") or [],
            "from_camera_id": edge.get("from_camera_id"),
        })
        scored.append(row)

    scored.sort(key=lambda r: (-r["evidence_ratio"], -r["score"]))
    return scored


# ── The full reconstruction ─────────────────────────────────────────────────

# The window is searched wider than the corridor predicts, on purpose. A hard edge at
# p90 makes a vehicle that hit one extra red light invisible; scoring it and letting
# the time factor penalise it produces a ranked lead instead of a silent miss.
#
# Widened *forwards only*. The slack is half the p10–p90 span, and for a geo edge that
# span is p10 * (free_speed / congested_speed) - p10 = 4.5 * p10 — so a symmetric slack
# opens the window 1.25 * p10 *before* the anchor's own timestamp, and the engine then
# looks for the subject at a downstream camera before it left the upstream one. Every
# candidate found there is a vehicle the subject cannot be, and one of them scored a
# posterior of 0.999997 because it carried the same plate: the same vehicle, on an
# earlier leg of its own journey, reported as a forward arrival. A subject cannot be
# observed downstream before it departs, so the window is clamped at the departure.
_WINDOW_SLACK = 0.5
# Bound on how many (camera, hop) cells get scored. Each one is a query, and a 5 km
# geo graph over 30 cameras is near-complete, so the tail of the expansion is cells
# with a corridor probability too small to change any ranking.
_MAX_CELLS = 40


async def run(
    anchor: Anchor,
    horizon_minutes: Optional[int] = None,
    max_hops: Optional[int] = None,
    min_evidence_ratio: Optional[float] = None,
    min_score: Optional[float] = None,
) -> dict:
    """Reconstruct where the subject went. Returns candidates and predictions.

    Pure computation — nothing is written here. Persistence is the caller's decision
    because the same engine serves a saved pursuit and a throwaway "what if" from the
    investigation panel, and an engine that writes on every call cannot serve the
    second.
    """
    horizon_s = float((horizon_minutes or settings.dragnet_horizon_minutes) * 60)
    hops = int(max_hops or settings.dragnet_max_hops)
    min_lr = float(min_evidence_ratio if min_evidence_ratio is not None
                   else settings.dragnet_min_evidence_ratio)
    min_sc = float(min_score if min_score is not None else settings.dragnet_min_score)

    edge_rows = await graph.edges()
    if not edge_rows:
        return {
            "candidates": [], "predictions": [], "cells_scored": 0,
            "warning": ("the camera graph is empty — run POST /investigate/graph/rebuild "
                        "before reconstructing, or there are no edges to follow"),
        }

    reach = await graph.expand(
        anchor.camera_id, anchor.ts, horizon_s, max_hops=hops,
        min_probability=0.005, edge_rows=edge_rows,
    )
    cells = reach[:_MAX_CELLS]

    backgrounds: dict[str, Background] = {}
    candidates: list[dict] = []
    exclude = [i for i in (anchor.track_uuid,) if i]

    for cell in cells:
        camera_id = str(cell["camera_id"])
        if camera_id not in backgrounds:
            backgrounds[camera_id] = await background_for(anchor, camera_id)

        span = (cell["eta_to"] - cell["eta_from"]).total_seconds()
        slack = timedelta(seconds=max(span * _WINDOW_SLACK, 30.0))
        # `max(..., anchor.ts)`: the earliest a forward hop can be observed is the
        # moment the subject was last seen at the origin. See _WINDOW_SLACK above.
        search_from = max(cell["eta_from"] - slack, anchor.ts)
        edge = {
            "camera_id": camera_id,
            "probability": cell["probability"],
            "evidence": cell.get("evidence"),
            "observations": cell.get("observations"),
            "hops": cell["hops"],
            "via": cell.get("via") or [],
            "from_camera_id": cell.get("from_camera_id"),
            "p10_transit_s": (cell["eta_from"] - anchor.ts).total_seconds(),
            "median_transit_s": (cell["eta_peak"] - anchor.ts).total_seconds(),
            "p90_transit_s": (cell["eta_to"] - anchor.ts).total_seconds(),
        }
        candidates.extend(await hop(
            anchor, edge,
            eta_from=search_from,
            eta_to=cell["eta_to"] + slack,
            horizon_s=horizon_s,
            background=backgrounds[camera_id],
            exclude=exclude,
        ))

    # One row per track. The same vehicle can be reachable at two hop counts (direct,
    # and via a detour); the stronger reading is the one to show, and reporting both
    # would put the same candidate on the timeline twice.
    best: dict[str, dict] = {}
    for cand in candidates:
        key = str(cand["track_uuid"])
        if key not in best or cand["evidence_ratio"] > best[key]["evidence_ratio"]:
            best[key] = cand

    kept = [
        c for c in best.values()
        if c["evidence_ratio"] >= min_lr and c["score"] >= min_sc
    ]
    kept.sort(key=lambda c: (-c["score"], -c["evidence_ratio"]))

    return {
        "candidates": kept,
        "predictions": cells,
        "cells_scored": len(cells),
        "cells_available": len(reach),
        "candidates_examined": len(candidates),
        "candidates_kept": len(kept),
        "thresholds": {"min_evidence_ratio": min_lr, "min_score": min_sc,
                       "horizon_minutes": horizon_s / 60, "max_hops": hops},
        "background_scopes": {k: v.scope for k, v in backgrounds.items()},
    }


def _rowcount(tag: str) -> int:
    """Row count out of an asyncpg command tag (``"DELETE 3"``)."""
    try:
        return int(str(tag).rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


async def persist(pursuit_id: str, anchor: Anchor, result: dict) -> dict:
    """Write the reconstruction to a pursuit. Re-runnable.

    Every candidate lands with ``confirmed = NULL``. That column is the boundary
    between what the engine computed and what a person accepted, and nothing in this
    module is allowed to cross it.
    """
    seed_seq = 0
    await db.execute(
        """
        INSERT INTO pursuit_sightings (pursuit_id, detection_id, track_uuid, camera_id, ts,
                                       pts_ms, score, match_kind, score_breakdown,
                                       thumbnail_key, confirmed, reviewed_by, sequence_no)
        VALUES ($1, $2, $3, $4, $5, NULL, 1.0, 'seed', $6::jsonb, $7, TRUE, $8, $9)
        ON CONFLICT (pursuit_id, detection_id) DO UPDATE
            SET score = 1.0, match_kind = 'seed', confirmed = TRUE
        """,
        pursuit_id, anchor.detection_id, anchor.track_uuid, anchor.camera_id, anchor.ts,
        {"seed": True, "note": "the sighting the operator started from"},
        anchor.thumbnail_key, "engine", seed_seq,
    )

    # Retract the engine's own previous output before writing the new. Without this a
    # re-run only ever *adds*: a candidate the engine no longer proposes — because a
    # threshold was raised, or because the scorer was fixed — stays on the operator's
    # timeline at whatever posterior the old run gave it, and the pursuit shows the
    # union of every version that ever ran. That is how three arrivals timed before
    # their own departure survived at 0.999997 after the corridor window was clamped.
    #
    # `confirmed IS NULL AND match_kind <> 'seed'` is the whole safety of it: a
    # reviewed row is a person's finding and is not the engine's to withdraw, and the
    # seed is the premise of the question. Both are left exactly where they are.
    # The count is returned rather than swallowed — a lead disappearing quietly is the
    # same dishonesty the expiry of predictions below exists to avoid.
    keep_detections = [
        c["best_detection_id"] for c in result["candidates"] if c.get("best_detection_id")
    ]
    retracted = _rowcount(await db.execute(
        """
        DELETE FROM pursuit_sightings
        WHERE pursuit_id = $1 AND confirmed IS NULL AND match_kind <> 'seed'
          AND NOT (detection_id = ANY($2::uuid[]))
        """,
        pursuit_id, keep_detections,
    ))

    written = 0
    ordered = sorted(result["candidates"], key=lambda c: c["first_seen"])
    for n, cand in enumerate(ordered, start=1):
        await db.execute(
            """
            INSERT INTO pursuit_sightings (pursuit_id, detection_id, track_uuid, camera_id,
                                           ts, pts_ms, score, match_kind, score_breakdown,
                                           thumbnail_key, sequence_no)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
            ON CONFLICT (pursuit_id, detection_id) DO UPDATE SET
                score = EXCLUDED.score,
                match_kind = EXCLUDED.match_kind,
                score_breakdown = EXCLUDED.score_breakdown,
                sequence_no = EXCLUDED.sequence_no
            """,
            pursuit_id, cand["best_detection_id"], cand["track_uuid"], cand["camera_id"],
            cand["first_seen"], cand["first_pts_ms"], float(cand["score"]),
            cand["match_kind"], cand["score_breakdown"],
            cand["thumbnail_key"], n,
        )
        written += 1

    # Predictions are the forward-looking half: cameras to watch, and when. Marked
    # expired rather than deleted on a re-run, so an operator can see that a window
    # they were given has closed instead of finding it silently gone.
    await db.execute(
        "UPDATE pursuit_predictions SET expired = TRUE WHERE pursuit_id = $1", pursuit_id
    )
    predicted = 0
    for cell in result["predictions"]:
        await db.execute(
            """
            INSERT INTO pursuit_predictions (pursuit_id, camera_id, from_camera_id,
                                             eta_from, eta_to, probability, hops, via, expired)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, FALSE)
            ON CONFLICT (pursuit_id, camera_id, hops) DO UPDATE SET
                from_camera_id = EXCLUDED.from_camera_id,
                eta_from = EXCLUDED.eta_from, eta_to = EXCLUDED.eta_to,
                probability = EXCLUDED.probability, via = EXCLUDED.via, expired = FALSE
            """,
            pursuit_id, str(cell["camera_id"]),
            str(cell["from_camera_id"]) if cell.get("from_camera_id") else None,
            cell["eta_from"], cell["eta_to"], float(cell["probability"]), int(cell["hops"]),
            [str(v) for v in (cell.get("via") or [])],
        )
        predicted += 1

    # A prediction whose camera already produced a kept candidate is resolved by it.
    await db.execute(
        """
        UPDATE pursuit_predictions p SET resolved_by = s.id
        FROM pursuit_sightings s
        WHERE p.pursuit_id = $1 AND s.pursuit_id = $1
          AND s.camera_id = p.camera_id AND s.match_kind <> 'seed'
          AND s.ts BETWEEN p.eta_from - INTERVAL '5 minutes'
                       AND p.eta_to   + INTERVAL '5 minutes'
          AND p.resolved_by IS NULL
        """,
        pursuit_id,
    )
    return {"sightings_written": written, "sightings_retracted": retracted,
            "predictions_written": predicted}


async def confirm(sighting_id: str, verdict: bool, operator: str) -> dict:
    """Record an operator's verdict, and let a confirmed hop teach the graph.

    This is the only path by which the corridor model improves. A confirmed sighting
    is a witnessed transition between two specific cameras with a witnessed transit
    time — the strongest edge evidence the system can obtain — and the edge is created
    if it did not exist, because a route nobody had seen before is precisely the thing
    worth remembering.

    A rejection is recorded and deliberately does *not* weaken the edge. The operator
    is saying "not this vehicle", not "that road does not connect"; treating a rejected
    candidate as evidence against the corridor would let one busy camera's false
    positives erode a route that genuinely exists.
    """
    # `match_kind <> 'seed'` in the WHERE, not a Python check afterwards: the seed row
    # is the pursuit's premise and is written confirmed. Letting it be reviewed FALSE
    # would both contradict the question being asked and break the _reinforce lookback
    # below, which walks backwards through `confirmed IS TRUE` to find the previous
    # camera and would then skip over the origin.
    row = await db.fetchrow(
        """
        UPDATE pursuit_sightings SET confirmed = $2, reviewed_by = $3
        WHERE id = $1 AND match_kind <> 'seed'
        RETURNING id, pursuit_id, camera_id, ts, score, match_kind, score_breakdown
        """,
        sighting_id, verdict, operator,
    )
    if row is None:
        exists = await db.fetchval(
            "SELECT match_kind FROM pursuit_sightings WHERE id = $1", sighting_id
        )
        return {"updated": False, "error": "seed" if exists == "seed" else "not_found"}

    learned = None
    if verdict:
        breakdown = row["score_breakdown"] or {}
        prior = breakdown.get("prior") or {}
        from_camera = None
        seq = await db.fetchrow(
            """
            SELECT camera_id, ts FROM pursuit_sightings
            WHERE pursuit_id = $1 AND confirmed IS TRUE AND ts < $2
            ORDER BY ts DESC LIMIT 1
            """,
            row["pursuit_id"], row["ts"],
        )
        if seq is not None and str(seq["camera_id"]) != str(row["camera_id"]):
            from_camera = str(seq["camera_id"])
            transit_s = (row["ts"] - seq["ts"]).total_seconds()
            if 1 <= transit_s <= settings.graph_transit_horizon_s:
                learned = await _reinforce(from_camera, str(row["camera_id"]), transit_s,
                                          int(prior.get("hops") or 1))
    return {
        "updated": True,
        "pursuit_id": str(row["pursuit_id"]),
        "camera_id": str(row["camera_id"]),
        "confirmed": verdict,
        "sighting": dict(row),
        "graph": learned,
    }


async def _reinforce(src: str, dst: str, transit_s: float, hops: int) -> dict:
    """Fold one witnessed transit into an edge.

    ``median_transit_s`` is nudged toward the observation rather than replaced, and the
    p10/p90 bounds are widened to contain it. Nudged, because one confirmation is one
    sample and the column is a distribution parameter, not a reading; widened, because
    a window that excludes an arrival the operator has confirmed will exclude the next
    one too. The percentiles are recomputed properly from all observations the next
    time the graph is rebuilt.

    Only a direct hop teaches an edge. A confirmation two hops downstream says the
    subject passed *somewhere* in between, and inventing a direct A→C edge from it
    would fabricate a road.
    """
    if hops != 1:
        return {"reinforced": False, "reason": f"confirmation was {hops} hops out"}
    await db.execute(
        """
        INSERT INTO camera_links (src_camera_id, dst_camera_id, observations,
                                  p10_transit_s, median_transit_s, p90_transit_s,
                                  probability, evidence, updated_at)
        VALUES ($1, $2, 1, $3 * 0.7, $3, $3 * 1.4, 0.2, 'confirmed', NOW())
        ON CONFLICT (src_camera_id, dst_camera_id) DO UPDATE SET
            observations     = camera_links.observations + 1,
            median_transit_s = (COALESCE(camera_links.median_transit_s, $3)
                                * camera_links.observations + $3)
                               / (camera_links.observations + 1),
            p10_transit_s    = LEAST(COALESCE(camera_links.p10_transit_s, $3), $3 * 0.9),
            p90_transit_s    = GREATEST(COALESCE(camera_links.p90_transit_s, $3), $3 * 1.1),
            evidence         = 'confirmed',
            updated_at       = NOW()
        """,
        src, dst, float(transit_s),
    )
    return {"reinforced": True, "src_camera_id": src, "dst_camera_id": dst,
            "observed_transit_s": round(transit_s, 1)}
