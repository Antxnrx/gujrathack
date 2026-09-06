"""Synthetic demo data for the Setu backend — cameras, detections, zones, tags.

Why this exists
---------------
Real inference output lives on the GPU box. Without it every table is empty, and
an empty database hides exactly the bugs that matter: the ``tracks`` upsert, the
``events`` dedupe key, the ``camera_index`` rollup, pgvector's ``avg()`` over a
group, and every SELECT the API is about to be built on. This script fills the
database with data that is *shaped* like real output so those paths get
exercised, and so the frontend has something to render before the GPU box is
reachable.

What is real and what is not
----------------------------
* ``cameras`` rows carry the **real** Sentinel coordinates from
  ``scripts/seed.py`` and the **real** playable HLS URL
  (``https://cctv.corp8.cloud/{id}/index.m3u8``). The streams work; only the
  metadata over them is invented.
* Every ``detections`` row is written with ``source = 'synthetic'``. Nothing
  downstream can confuse it for inference output, and ``--reset`` can find it
  again.
* ``tracks`` and ``events`` are **not** written here. They are produced by
  running ``python -m ml.analytics.runner`` over this data, which is the point:
  the fold and the rule engine do the work they will do in production.

Honesty about the embeddings
----------------------------
Embeddings are derived from CLIP *text* encodings of each (class, colour) pair,
so they sit in the same vector space as the image embeddings the pipeline
writes — natural-language search ranks them plausibly instead of returning
noise. They are then perturbed to *reproduce CLIP's documented weakness* rather
than hide it:

    two different red cars          cosine ≈ 0.81   (real range: 0.76–0.91)
    same vehicle, two cameras       cosine ≈ 0.92
    same vehicle, two frames        cosine ≈ 0.97

``ml/watchlist.py`` disabled embedding identity matching because that first
number is so high. If the synthetic data made different vehicles look far apart,
Dragnet's ranking would look flawless here and fail on real footage. The gap
between 0.81 and 0.92 is small on purpose: it is the gap a likelihood-ratio
score can exploit and an absolute threshold cannot.

The OCR noise model is deliberately *not* imported from ``ml/analytics/anpr.py``.
Generating errors with the same confusion table the solver corrects for would be
circular. This module keeps its own, wider table and adds unmodelled
substitutions, dropped and inserted characters, so consensus has to recover
plates from errors it has no prior for.

Usage
-----
    python scripts/seed_demo.py                     # ~4 h of traffic, 30 cameras
    python scripts/seed_demo.py --hours 8 --density 9
    python scripts/seed_demo.py --seed 7 --reset    # wipe synthetic rows, re-seed
    python scripts/seed_demo.py --json              # machine-readable report

Then fold and score it:

    python -m ml.analytics.runner --session-key demo-7 -v
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import math
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ml.analytics.rules import RuleParams

log = logging.getLogger("seed_demo")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SENTINEL_BASE = "https://cctv.corp8.cloud"


# ═══════════════════════════════════════════════════════════════════════════
# Camera catalogue
# ═══════════════════════════════════════════════════════════════════════════
# Coordinates are *not* duplicated here — they are read out of
# ``scripts/seed.py``, which is the source of truth for camera provisioning, by
# parsing its AST rather than importing it. Importing would pull in
# ``ml.auth``/``requests`` and require Sentinel credentials to seed an offline
# database, which is backwards. Parsing keeps one copy of the coordinates and
# fails loudly if the table is renamed.
#
# What this module *does* own is the metadata Sentinel's ``cameras.json`` never
# returns (it returns ``{"id", "name"}`` and nothing else): district, area and
# owning department. Districts are inferred from the coordinates in seed.py and
# are demo metadata — ``geocode_precision`` stays 'district', matching Model 1's
# contract, which is also why nothing here computes a distance between two
# cameras.

# external_id → (display name, district, area, department)
CAMERA_META: dict[str, tuple[str, str, str, str]] = {
    "cam01": ("01 Chiman Bhai Bridge", "Ahmedabad", "Sabarmati Riverfront", "police"),
    "cam02": ("02 Janpath", "Ahmedabad", "Central", "police"),
    "cam03": ("03 ONGC Office", "Ahmedabad", "Chandkheda", "police"),
    "cam04": ("04 Paldi Circle", "Ahmedabad", "Paldi", "police"),
    "cam05": ("05 Visat Teen Rasta", "Ahmedabad", "Visat", "highways"),
    "cam06": ("06 Timbavadi Gate", "Junagadh", "Timbavadi", "police"),
    "cam07": ("07 Hero Showroom", "Gir Somnath", "Veraval Road", "police"),
    "cam08": ("08 Majewadi Gate", "Junagadh", "Majewadi", "police"),
    "cam09": ("09 New Bypass", "Junagadh", "Bypass", "highways"),
    "cam10": ("10 Char Chowk Road", "Junagadh", "Char Chowk", "municipal"),
    "cam11": ("11 Dolatpara", "Junagadh", "Dolatpara", "police"),
    "cam12": ("12 Tri Mandir Adalaj Tollnaka", "Gandhinagar", "Adalaj", "highways"),
    "cam13": ("13 CN Vidhyalaya", "Ahmedabad", "Ambawadi", "municipal"),
    "cam14": ("14 Delight RLVD", "Ahmedabad", "Navrangpura", "police"),
    "cam15": ("15 Suvidha Park", "Ahmedabad", "Paldi", "municipal"),
    "cam16": ("16 Visat P2", "Ahmedabad", "Visat", "highways"),
    "cam17": ("17 Rajkot Bus Port", "Rajkot", "Bus Port", "municipal"),
    "cam18": ("18 Rajkot City", "Rajkot", "Central", "police"),
    "cam19": ("19 Khaparia", "Navsari", "Khaparia", "highways"),
    "cam20": ("20 Mohanpura", "Kheda", "Mohanpura", "police"),
    "cam21": ("21 Dethali Char Rasta", "Patan", "Dethali", "highways"),
    "cam22": ("22 BK Mervada Tran Rasta", "Ahmedabad", "Mervada", "police"),
    "cam23": ("23 Kheram", "Kheda", "Kheram", "police"),
    "cam24": ("24 Dehgam", "Gandhinagar", "Dehgam", "police"),
    "cam25": ("25 Dhanori", "Gandhinagar", "Dhanori", "municipal"),
    "cam26": ("26 Tankal", "Gandhinagar", "Tankal", "police"),
    "cam27": ("27 Bilimora North", "Navsari", "Bilimora", "municipal"),
    "cam28": ("28 Bilimora Market", "Navsari", "Bilimora", "municipal"),
    "cam29": ("29 Bilimora Station", "Navsari", "Bilimora", "highways"),
    "cam30": ("30 Gandhidham Rambagh", "Kutch", "Rambagh", "police"),
}

# Model 2's brief asks for feeds from *at least two different systems*. The
# sandbox serves them all from one gateway, so the departmental split is carried
# in metadata rather than pretended at the transport layer: the architecture note
# describes how a second VMS attaches, and this is the column it attaches on.
# (label, source_system, uid_code). ``source_system`` names the *departmental
# estate* a camera belongs to, which is what Model 2 federates across — not the
# transport, which in this sandbox is one shared HLS gateway for all thirty
# cameras. Those are different facts and the schema keeps them apart:
# ``metadata.source_system`` says whose cameras these are, ``ingest_url`` says
# how this deployment reaches them. Claiming three different VMS *products* on
# the strength of one gateway would be inventing a deliverable; naming three
# departmental estates is just describing the ones that exist.
DEPARTMENTS = {
    "police":    ("Gujarat Police", "Gujarat Police — Sentinel VMS", "POL"),
    "municipal": ("Municipal Corporation", "AMC City Surveillance", "MUN"),
    "highways":  ("State Highways Authority", "SHA Highway Cameras", "HWY"),
}

# Not every camera is 1080p in the field, and zone polygons are stored
# normalised precisely so that stops mattering. Mixing resolutions here means
# the normalisation is actually under test instead of being assumed.
FRAME_SIZES = [(1920, 1080), (1280, 720)]


def load_camera_coords() -> dict[str, tuple[float, float]]:
    """Read ``CAMERA_COORDS`` out of scripts/seed.py without importing it."""
    src = (REPO_ROOT / "scripts" / "seed.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:
        targets = getattr(node, "targets", []) or (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "CAMERA_COORDS":
                return {k: tuple(v) for k, v in ast.literal_eval(node.value).items()}
    raise RuntimeError(
        "CAMERA_COORDS not found in scripts/seed.py — the coordinate table moved. "
        "seed_demo.py reads it from there on purpose so there is only one copy."
    )


# ═══════════════════════════════════════════════════════════════════════════
# The road network
# ═══════════════════════════════════════════════════════════════════════════
# Ordered camera sequences with per-hop transit times in seconds. These are
# invented — a demo corridor, not surveyed road geometry. They exist so that
# vehicles make *journeys* across cameras instead of appearing at one and
# vanishing, which is what makes the camera-transition graph learnable and
# cross-camera investigation demonstrable.
#
# Nothing downstream is allowed to read this table. The graph builder re-derives
# transit distributions from the seeded observations, exactly as it will from
# real ones — so if these numbers were wrong, the learned graph would disagree
# with them and that disagreement would be visible rather than hidden.
#
# cam21 (Patan) and cam30 (Kutch) are deliberately in no corridor: an isolated
# camera must not acquire invented neighbours.
CORRIDORS: list[tuple[str, list[str], list[float]]] = [
    ("Ahmedabad North Spine",
     ["cam12", "cam05", "cam16", "cam03", "cam02", "cam14", "cam13", "cam01", "cam04"],
     [480, 70, 300, 540, 180, 130, 240, 250]),
    ("Ahmedabad East Approach",
     ["cam24", "cam25", "cam26", "cam22", "cam03"],
     [360, 300, 660, 540]),
    ("Ahmedabad Inner Ring",
     ["cam15", "cam04", "cam01", "cam13"],
     [200, 250, 240]),
    ("Junagadh Ring",
     ["cam09", "cam08", "cam06", "cam10", "cam11"],
     [240, 190, 260, 300]),
    ("Junagadh–Somnath Highway",
     ["cam11", "cam06", "cam07"],
     [260, 1320]),
    ("Rajkot City", ["cam17", "cam18"], [360]),
    ("Navsari Coastal", ["cam19", "cam27", "cam28", "cam29"], [840, 140, 190]),
    ("Kheda Link", ["cam20", "cam23"], [720]),
]


# ═══════════════════════════════════════════════════════════════════════════
# Traffic composition
# ═══════════════════════════════════════════════════════════════════════════
# Class mix approximates Gujarat urban traffic; colour is conditioned on class
# because it is not independent of it — autos are yellow and green, buses are
# municipal red and orange. Search only feels real if "yellow auto rickshaw"
# returns autos rather than a uniform sample of everything.
CLASS_MIX: list[tuple[str, float]] = [
    ("motorcycle", 0.32), ("car", 0.26), ("auto_rickshaw", 0.13),
    ("truck", 0.11), ("person", 0.09), ("bus", 0.05), ("tractor", 0.04),
]

COLOR_BY_CLASS: dict[str, list[tuple[str, float]]] = {
    "auto_rickshaw": [("yellow", .42), ("green", .34), ("black", .12), ("blue", .12)],
    "bus":           [("red", .30), ("white", .28), ("orange", .22), ("blue", .20)],
    "truck":         [("blue", .30), ("orange", .20), ("white", .20), ("red", .15), ("yellow", .15)],
    "motorcycle":    [("black", .34), ("silver", .20), ("red", .16), ("blue", .15), ("white", .15)],
    "car":           [("white", .34), ("silver", .24), ("black", .16), ("red", .10), ("blue", .10), ("orange", .06)],
    "tractor":       [("blue", .40), ("red", .32), ("orange", .28)],
    "person":        [("black", .24), ("white", .22), ("blue", .20), ("silver", .18), ("red", .16)],
    "suitcase":      [("black", .55), ("silver", .25), ("blue", .20)],
    "backpack":      [("black", .50), ("blue", .30), ("red", .20)],
}

# Box aspect (width / height) per class, used to build plausible bboxes.
ASPECT: dict[str, float] = {
    "motorcycle": 0.80, "car": 1.55, "auto_rickshaw": 1.00, "truck": 1.40,
    "bus": 1.45, "tractor": 1.25, "person": 0.42, "suitcase": 0.85,
    "backpack": 0.70, "handbag": 0.95,
}

# Apparent height, normalised to frame height, at the far and near end of a pass.
HEIGHT_RANGE: dict[str, tuple[float, float]] = {
    "motorcycle": (0.055, 0.19), "car": (0.070, 0.24), "auto_rickshaw": (0.065, 0.21),
    "truck": (0.100, 0.34), "bus": (0.110, 0.36), "tractor": (0.085, 0.27),
    "person": (0.075, 0.26), "suitcase": (0.030, 0.075), "backpack": (0.028, 0.065),
    "handbag": (0.022, 0.055),
}

PLATED_CLASSES = {"car", "truck", "bus", "auto_rickshaw", "tractor", "motorcycle"}
OBJECT_CLASSES = {"suitcase", "backpack", "handbag"}

# Gujarat RTO codes that actually exist, plus a few out-of-state series so the
# plate search has inter-state traffic in it.
GJ_RTO = ["01", "02", "03", "05", "06", "07", "09", "12", "16", "18", "21", "27", "38"]
OUT_OF_STATE = [("MH", ["01", "02", "12", "43"]), ("RJ", ["14", "19", "27"]),
                ("DL", ["01", "03", "08"]), ("MP", ["09", "13"])]
PLATE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # I and O omitted, as issuers do


# ═══════════════════════════════════════════════════════════════════════════
# Plates and the OCR noise model
# ═══════════════════════════════════════════════════════════════════════════
# This table is intentionally NOT ml/analytics/anpr.py's ``_CONFUSION_CLASSES``.
# Generating errors with the same table the solver corrects for would make the
# recovery rate a statement about nothing. This one is wider, and 20% of
# substitutions are drawn from outside it entirely, so consensus has to survive
# errors it holds no prior for.
_OCR_SWAPS: dict[str, str] = {
    "0": "OQD8", "O": "0QDU", "D": "0O", "Q": "0O",
    "1": "IL7T", "I": "1LT", "L": "1I", "T": "17I",
    "2": "Z7", "Z": "27", "5": "S6", "S": "58",
    "8": "B60", "B": "8R", "6": "5G8", "G": "6C0",
    "4": "AH", "A": "4R", "7": "1T2", "9": "PQ4",
    "P": "9RF", "R": "PB8", "U": "VO", "V": "UY",
    "C": "GO0", "E": "FB", "F": "EP", "M": "NHW",
    "N": "MHV", "H": "MNK", "K": "XH", "W": "MV",
    "X": "KY", "Y": "VX", "3": "8B", "J": "1I",
}
_ALPHANUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def make_plate(rng: random.Random) -> str:
    """A syntactically legal Indian plate: ``LL DD L{1,3} DDDD``."""
    if rng.random() < 0.16:
        state, codes = rng.choice(OUT_OF_STATE)
        rto = rng.choice(codes)
    else:
        state, rto = "GJ", rng.choice(GJ_RTO)
    series_len = rng.choices([1, 2, 3], weights=[0.10, 0.72, 0.18])[0]
    series = "".join(rng.choice(PLATE_LETTERS) for _ in range(series_len))
    return f"{state}{rto}{series}{rng.randint(1, 9999):04d}"


def corrupt_plate(plate: str, rng: random.Random, severity: float) -> str:
    """One frame's OCR read of *plate*.

    *severity* in 0–1 scales how badly this frame was seen: a small, blurred,
    off-angle plate loses more characters than a large square-on one. Errors are
    structured (glyph confusions) with an unmodelled tail, plus deletions and the
    occasional spurious insertion — the three failure modes that break naive
    position-by-position voting.
    """
    chars = list(plate)
    # Substitution is per *character*, so the rate compounds over a 10-character
    # plate — 0.31/char would leave 2% of reads intact and make the OCR worse than
    # any real engine. Calibrated instead to published ANPR behaviour on CCTV:
    # roughly 95% per-character on a legible plate falling to ~78% on a marginal
    # one, which is 60% down to 9% of whole plates read exactly.
    for i, ch in enumerate(chars):
        if rng.random() < 0.02 + 0.26 * severity:
            if rng.random() < 0.80 and ch in _OCR_SWAPS:
                chars[i] = rng.choice(_OCR_SWAPS[ch])
            else:
                chars[i] = rng.choice(_ALPHANUM)
    # Deletion and insertion are per *read*: they are framing failures — a
    # character cropped off the edge, a bolt read as a digit — not per-character
    # noise. They are what breaks position-by-position voting, and why the
    # consensus aligner exists.
    if chars and rng.random() < 0.05 + 0.18 * severity:      # dropped character
        del chars[rng.randrange(len(chars))]
    if chars and rng.random() < 0.03 + 0.08 * severity:      # spurious character
        at = rng.randrange(len(chars) + 1)
        chars.insert(at, rng.choice(_ALPHANUM))
    return "".join(chars)


# ═══════════════════════════════════════════════════════════════════════════
# Embeddings
# ═══════════════════════════════════════════════════════════════════════════
# Three nested levels of similarity, each set by *exact* cosine construction
# rather than by guessing a noise σ:
#
#   prompt   →  identity   (cos ~0.90)   this vehicle vs the class/colour ideal
#   identity →  sighting   (cos ~0.95)   this camera's view of that vehicle
#   sighting →  frame      (cos ~0.985)  one frame of that view
#
# The multiplicative consequence is the point (cosines compose in high
# dimension because independent perturbations are near-orthogonal):
#
#   different vehicles, same class+colour   0.90² ≈ 0.81
#   same vehicle at two cameras             0.95² ≈ 0.90
#   same vehicle in two frames              0.985² ≈ 0.97
#
# which is the real, uncomfortable geometry ml/watchlist.py measured.

IDENTITY_COS = (0.86, 0.93)
SIGHTING_COS = (0.93, 0.975)
FRAME_COS = (0.975, 0.992)


class EmbeddingFactory:
    """CLIP text embeddings for each (class, colour) pair, plus perturbations.

    Uses ``ml.models.get_clip()`` — the same ViT-B-32 / ``pretrained="openai"``
    weights the pipeline encodes images with. Loading a different checkpoint here
    would put the synthetic vectors in a different space and silently wreck
    search ranking, which is the failure the backend's ``clip_text`` module has
    to avoid too.
    """

    def __init__(self, rng_seed: int):
        import numpy as np
        self.np = np
        self.rng = np.random.default_rng(rng_seed)
        self._base: dict[tuple[str, str], "np.ndarray"] = {}

    def load(self, classes: list[str], colors: list[str]) -> None:
        import numpy as np
        import open_clip
        import torch

        from ml.models import device, get_clip

        model, _ = get_clip()
        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        pairs = [(c, k) for c in classes for k in colors]
        prompts = [self._prompt(c, k) for c, k in pairs]
        with torch.no_grad():
            toks = tokenizer(prompts).to(device)
            embs = model.encode_text(toks)
            embs /= embs.norm(dim=-1, keepdim=True)
        arr = embs.cpu().numpy().astype(np.float32)
        for (c, k), v in zip(pairs, arr):
            self._base[(c, k)] = v
        log.info("Encoded %d (class, colour) prompts with CLIP ViT-B-32", len(pairs))

    @staticmethod
    def _prompt(cls: str, color: str) -> str:
        readable = cls.replace("_", " ")
        if cls == "person":
            return f"a photo of a person in {color} clothing walking on a street"
        if cls in OBJECT_CLASSES:
            return f"a photo of a {color} {readable} left on the ground"
        return f"a photo of a {color} {readable} on an Indian city road"

    def perturb(self, vec, cos: float):
        """A unit vector at exactly *cos* cosine similarity to *vec*."""
        np = self.np
        n = self.rng.standard_normal(vec.shape[0]).astype(np.float32)
        n -= float(n @ vec) * vec
        norm = float(np.linalg.norm(n))
        if norm < 1e-6:
            return vec.copy()
        n /= norm
        out = cos * vec + math.sqrt(max(0.0, 1.0 - cos * cos)) * n
        return out / float(np.linalg.norm(out))

    def base(self, cls: str, color: str):
        key = (cls, color)
        if key not in self._base:
            key = (cls, "white") if (cls, "white") in self._base else next(iter(self._base))
        return self._base[key]

    def identity(self, cls: str, color: str, rng: random.Random):
        return self.perturb(self.base(cls, color), rng.uniform(*IDENTITY_COS))

    def sighting(self, identity_vec, rng: random.Random):
        return self.perturb(identity_vec, rng.uniform(*SIGHTING_COS))

    def frame(self, sighting_vec, rng: random.Random) -> list[float]:
        return self.perturb(sighting_vec, rng.uniform(*FRAME_COS)).tolist()


# ═══════════════════════════════════════════════════════════════════════════
# Camera geometry
# ═══════════════════════════════════════════════════════════════════════════
# Each camera gets a *road band*: an axis from an entry point to an exit point
# in normalised image coordinates, with a lateral half-width. Traffic is confined
# to it. That is not decoration — a restricted zone is only meaningful if normal
# traffic doesn't drive through it, and traffic scattered uniformly over the
# frame would make every zone fire on everything.


@dataclass
class CameraCtx:
    external_id: str
    uuid: str
    name: str
    frame_w: int
    frame_h: int
    entry: tuple[float, float]
    exit: tuple[float, float]
    half_w: float
    kind: str = "road"          # road | junction | toll
    busy: bool = False
    next_track_id: int = 1

    @property
    def axis(self) -> tuple[float, float]:
        dx = self.exit[0] - self.entry[0]
        dy = self.exit[1] - self.entry[1]
        n = math.hypot(dx, dy) or 1.0
        return (dx / n, dy / n)

    @property
    def normal(self) -> tuple[float, float]:
        ax, ay = self.axis
        return (-ay, ax)

    @property
    def flow_deg(self) -> float:
        """Heading of legal travel along the axis, in geometry.py's convention."""
        dx, dy = self.exit[0] - self.entry[0], self.exit[1] - self.entry[1]
        return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0

    def take_track_id(self) -> int:
        self.next_track_id += 1
        return self.next_track_id - 1


def build_camera_geometry(ctx: CameraCtx, rng: random.Random) -> None:
    """Give a camera a plausible road view: roughly frame-vertical, either
    approaching the lens or receding from it, with some skew."""
    approaching = rng.random() < 0.6
    near = (0.5 + rng.uniform(-0.18, 0.18), rng.uniform(0.86, 0.95))
    far = (0.5 + rng.uniform(-0.22, 0.22), rng.uniform(0.14, 0.24))
    ctx.entry, ctx.exit = (far, near) if approaching else (near, far)
    ctx.half_w = rng.uniform(0.06, 0.11)


def road_point(ctx: CameraCtx, u: float, lateral: float) -> tuple[float, float]:
    """Point at fraction *u* along the road axis, offset *lateral* across it."""
    ax, ay = ctx.axis
    nx, ny = ctx.normal
    x = ctx.entry[0] + (ctx.exit[0] - ctx.entry[0]) * u + nx * lateral
    y = ctx.entry[1] + (ctx.exit[1] - ctx.entry[1]) * u + ny * lateral
    return (min(0.99, max(0.01, x)), min(0.99, max(0.01, y)))


def apparent_height(cls: str, gy: float) -> float:
    """Normalised box height at ground-depth *gy*.

    Height follows the ground point's y rather than being ramped independently,
    which is what makes an approaching vehicle grow and a receding one shrink
    without either being special-cased.
    """
    lo, hi = HEIGHT_RANGE.get(cls, (0.06, 0.22))
    d = min(1.0, max(0.0, gy)) ** 1.35
    return lo + (hi - lo) * d


# ═══════════════════════════════════════════════════════════════════════════
# Trajectories — each returns [(pts_seconds, ground_x, ground_y), …]
# ═══════════════════════════════════════════════════════════════════════════

Sample = tuple[float, float, float]


def traj_transit(ctx: CameraCtx, t0: float, dur: float, fps: float,
                 rng: random.Random, reverse: bool = False,
                 lateral: Optional[float] = None) -> list[Sample]:
    """A pass along the road band.

    *lateral* is lane discipline and it matters: a direction zone covers one
    lane, not the whole carriageway, so oncoming traffic must actually be on the
    other side of the axis or every legal vehicle going the other way would be
    flagged as driving the wrong way. Wobble is capped well below the lane
    separation for the same reason.
    """
    lat = ctx.half_w * rng.uniform(-0.85, 0.85) if lateral is None else lateral
    wobble_amp = rng.uniform(0.0, 0.006)
    wobble_phase = rng.uniform(0, math.tau)
    n = max(4, int(dur * fps))
    out: list[Sample] = []
    for i in range(n):
        f = i / (n - 1)
        u = (1.0 - f) if reverse else f
        wob = wobble_amp * math.sin(wobble_phase + 3.0 * math.pi * f)
        gx, gy = road_point(ctx, u, lat + wob)
        out.append((t0 + i / fps, gx, gy))
    return out


def traj_static(x: float, y: float, t0: float, dur: float, fps: float,
                sigma: float, rng: random.Random) -> list[Sample]:
    n = max(4, int(dur * fps))
    return [(t0 + i / fps,
             min(0.99, max(0.01, x + rng.gauss(0, sigma))),
             min(0.99, max(0.01, y + rng.gauss(0, sigma))))
            for i in range(n)]


def traj_wander(x: float, y: float, radius: float, t0: float, dur: float,
                fps: float, rng: random.Random) -> list[Sample]:
    """A slow random walk kept inside *radius* of its start — pacing, not
    travelling. This is the trajectory that separates loitering from queueing."""
    n = max(4, int(dur * fps))
    out: list[Sample] = []
    px, py = x, y
    vx, vy = 0.0, 0.0
    for i in range(n):
        vx = 0.75 * vx + rng.gauss(0, radius * 0.10)
        vy = 0.75 * vy + rng.gauss(0, radius * 0.10)
        px, py = px + vx, py + vy
        d = math.hypot(px - x, py - y)
        if d > radius:                      # reflect back toward the anchor
            px, py = x + (px - x) * radius / d, y + (py - y) * radius / d
            vx, vy = -vx * 0.5, -vy * 0.5
        out.append((t0 + i / fps, min(0.99, max(0.01, px)), min(0.99, max(0.01, py))))
    return out


def traj_approach_then_dwell(start: tuple[float, float], end: tuple[float, float],
                             t0: float, walk_s: float, dwell_s: float, fps: float,
                             sigma: float, rng: random.Random) -> list[Sample]:
    out: list[Sample] = []
    n = max(3, int(walk_s * fps))
    for i in range(n):
        f = i / max(1, n - 1)
        out.append((t0 + i / fps,
                    start[0] + (end[0] - start[0]) * f,
                    start[1] + (end[1] - start[1]) * f))
    out.extend(traj_static(end[0], end[1], t0 + walk_s, dwell_s, fps, sigma, rng))
    return out


def traj_converge_and_stop(ctx: CameraCtx, start_u: float, start_lat: float,
                           meet: tuple[float, float], t0: float, run_s: float,
                           still_s: float, fps: float,
                           rng: random.Random) -> list[Sample]:
    """Travel to *meet*, arrive at the contact instant, then hold position.

    The jitter after contact is deliberately tiny: the accident rule requires
    post-contact speed below 0.004 normalised units per second, and 0.5 s
    sampling turns a σ of 0.001 into roughly 0.0035. Anything looser would look
    like two vehicles still creeping, which is what the rule is built to reject.
    """
    sx, sy = road_point(ctx, start_u, start_lat)
    n = max(6, int(run_s * fps))
    out: list[Sample] = []
    for i in range(n):
        f = i / (n - 1)
        e = f * f * (3 - 2 * f)             # ease-out: braking, not teleporting
        out.append((t0 + i / fps, sx + (meet[0] - sx) * e, sy + (meet[1] - sy) * e))
    out.extend(traj_static(meet[0], meet[1], t0 + run_s + 1.0 / fps,
                           still_s, fps, 0.0007, rng))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Frame-level noise
# ═══════════════════════════════════════════════════════════════════════════
# The pipeline labels class and colour per *frame*, with CLIP, on a crop that is
# sometimes 20 px tall. It gets them wrong on the small ones. Reproducing that
# is the point: it is the reason track-level aggregation exists, and a synthetic
# set where every frame agrees would make ``_weighted_vote`` untested and the
# ``class_votes`` column meaningless.
CLASS_CONFUSION: dict[str, list[str]] = {
    "car": ["truck", "auto_rickshaw"],
    "truck": ["bus", "car"],
    "bus": ["truck"],
    "motorcycle": ["person", "auto_rickshaw"],
    "auto_rickshaw": ["car", "motorcycle"],
    "tractor": ["truck"],
    "person": ["motorcycle"],
    "suitcase": ["backpack"],
    "backpack": ["suitcase"],
}
COLOR_CONFUSION: dict[str, list[str]] = {
    "white": ["silver"], "silver": ["white", "blue"], "black": ["blue"],
    "blue": ["black", "silver"], "red": ["orange"], "orange": ["red", "yellow"],
    "yellow": ["orange", "green"], "green": ["yellow", "blue"],
}


@dataclass
class SynthDetection:
    id: str
    camera_id: str
    track_id: int
    class_name: str
    bbox: dict
    confidence: float
    plate_text: Optional[str]
    plate_confidence: Optional[float]
    embedding: list
    ts: datetime
    pts_ms: float
    dominant_color: Optional[str]
    frame_w: int
    frame_h: int


@dataclass
class SynthTrack:
    """Book-keeping for one emitted track — the ground truth this script knows
    and the analytics layer does not. Written to the report so plate consensus
    can be *scored* instead of merely admired."""
    camera: str
    track_id: int
    class_name: str
    color: str
    plate: Optional[str]
    reads: list[str] = field(default_factory=list)
    journey: Optional[str] = None
    behaviour: str = "transit"
    first_pts_s: float = 0.0
    detections: int = 0


def emit_track(
    ctx: CameraCtx,
    samples: list[Sample],
    cls: str,
    color: str,
    plate: Optional[str],
    sighting_vec,
    ef: "EmbeddingFactory",
    rng: random.Random,
    session_start: datetime,
    *,
    behaviour: str = "transit",
    journey: Optional[str] = None,
    plate_read_rate: float = 0.75,
) -> tuple[list[SynthDetection], SynthTrack]:
    """Turn a trajectory into detection rows as the pipeline would have written
    them, and return the ground truth alongside."""
    track_id = ctx.take_track_id()
    lo, hi = HEIGHT_RANGE.get(cls, (0.06, 0.22))
    dets: list[SynthDetection] = []
    truth = SynthTrack(camera=ctx.external_id, track_id=track_id, class_name=cls,
                       color=color, plate=plate, journey=journey,
                       behaviour=behaviour, first_pts_s=samples[0][0])

    for pts_s, gx, gy in samples:
        h_norm = apparent_height(cls, gy)
        w_norm = h_norm * ASPECT.get(cls, 1.0)
        # Nearness in 0–1 across this class's own size range: drives detection
        # confidence and plate legibility together, because they have the same
        # underlying cause.
        nearness = (h_norm - lo) / (hi - lo) if hi > lo else 0.5

        x1 = (gx - w_norm / 2) * ctx.frame_w
        x2 = (gx + w_norm / 2) * ctx.frame_w
        y2 = gy * ctx.frame_h
        y1 = y2 - h_norm * ctx.frame_h
        bbox = {
            "x1": int(max(0, min(ctx.frame_w - 1, x1))),
            "y1": int(max(0, min(ctx.frame_h - 1, y1))),
            "x2": int(max(1, min(ctx.frame_w, x2))),
            "y2": int(max(1, min(ctx.frame_h, y2))),
        }
        if bbox["x2"] - bbox["x1"] < 8 or bbox["y2"] - bbox["y1"] < 10:
            continue                        # the pipeline drops crops this small

        conf = min(0.97, max(0.30, 0.40 + 0.52 * nearness + rng.gauss(0, 0.035)))

        frame_cls, frame_color = cls, color
        if conf < 0.58 and rng.random() < 0.20:
            frame_cls = rng.choice(CLASS_CONFUSION.get(cls, [cls]))
        if rng.random() < 0.12:
            frame_color = rng.choice(COLOR_CONFUSION.get(color, [color]))

        plate_text = plate_conf = None
        if plate and cls in PLATED_CLASSES and nearness > 0.40 and rng.random() < plate_read_rate:
            severity = min(0.95, max(0.10, 1.15 - nearness))
            plate_text = corrupt_plate(plate, rng, severity)
            plate_conf = round(min(0.92, max(0.30, 0.90 - 0.55 * severity + rng.gauss(0, 0.05))), 4)
            truth.reads.append(plate_text)

        dets.append(SynthDetection(
            id=str(uuid.uuid4()),
            camera_id=ctx.uuid,
            track_id=track_id,
            class_name=frame_cls,
            bbox=bbox,
            confidence=round(conf, 4),
            plate_text=plate_text,
            plate_confidence=plate_conf,
            embedding=ef.frame(sighting_vec, rng),
            # Written as a live worker would have: ts advances with the video
            # clock. A batch pass compresses ts into its own runtime, which is
            # correct for it and useless for a time-range search demo.
            ts=session_start + timedelta(seconds=pts_s),
            pts_ms=round(pts_s * 1000.0, 1),
            dominant_color=frame_color,
            frame_w=ctx.frame_w,
            frame_h=ctx.frame_h,
        ))

    truth.detections = len(dets)
    return dets, truth


# ═══════════════════════════════════════════════════════════════════════════
# Zones
# ═══════════════════════════════════════════════════════════════════════════
# Only the two kinds the rule engine actually reads are seeded: 'direction' for
# wrong-way and 'restricted' for intrusion. Seeding a 'crowd' polygon would look
# tidy on a map and be read by nothing — the crowd rule counts distinct person
# tracks camera-wide, on purpose, because a crowd that forms just outside the
# polygon is still a crowd.

# Lane fractions of half_w. Forward traffic keeps to one side of the axis,
# oncoming to the other, and the direction zone covers the forward lane only.
LANE_MIN, LANE_MAX = 0.35, 0.95
FOOTPATH = (1.15, 1.75)     # pedestrians, in units of half_w

# Anything that must be *clear of people* has to sit outside the pedestrian band
# by more than the rule's own clearance radius — otherwise background pedestrians
# walk through it and the rule correctly refuses to fire. Imported rather than
# hardcoded: if the threshold is retuned, the scenario follows it instead of
# quietly becoming untestable. The 0.04 is slack, so a pedestrian at the very
# edge of the outermost footpath lane is still outside the radius.
_CLEARANCE = RuleParams().abandoned_person_clearance_norm
_ZONE_HALF_DEPTH = 0.065


def clear_lateral(half_w: float) -> float:
    """Lateral offset (normalised, unsigned) that no background pedestrian
    reaches, with the clearance radius to spare."""
    return half_w * FOOTPATH[1] + _CLEARANCE + 0.04


def direction_zone(ctx: CameraCtx) -> dict:
    """A quad over the forward lane, carrying the legal heading."""
    poly = [
        list(road_point(ctx, 0.26, 0.0)),
        list(road_point(ctx, 0.80, 0.0)),
        list(road_point(ctx, 0.80, ctx.half_w * 1.05)),
        list(road_point(ctx, 0.26, ctx.half_w * 1.05)),
    ]
    return {
        "name": f"{ctx.name.split(' ', 1)[-1]} — carriageway lane A",
        "kind": "direction",
        "polygon": poly,
        "direction_deg": round(ctx.flow_deg, 1),
        "tolerance_deg": 70.0,
        # Pedestrians cross lanes at right angles. A wrong-way rule without a
        # class filter calls every one of them a violation.
        "classes": ["car", "motorcycle", "bus", "truck", "auto_rickshaw", "tractor"],
        "params": {},
    }


def restricted_zone(ctx: CameraCtx) -> Optional[dict]:
    """A quad set back from the carriageway — a compound edge or a footpath.

    Placed off the road band deliberately. A restricted polygon drawn across the
    road would fire on every passing vehicle, which is not a detector, it is a
    counter.

    Its near edge also clears the *pedestrian* band, for the same reason one step
    further out: a polygon whose inner edge overlaps the outermost footpath lane
    fires on people lawfully walking past, and an intrusion event that every
    pedestrian triggers tells an operator nothing.
    """
    for side in (1.0, -1.0):
        offset = side * (clear_lateral(ctx.half_w) + _ZONE_HALF_DEPTH)
        corners = [
            road_point(ctx, 0.34, offset - side * _ZONE_HALF_DEPTH),
            road_point(ctx, 0.66, offset - side * _ZONE_HALF_DEPTH),
            road_point(ctx, 0.66, offset + side * _ZONE_HALF_DEPTH),
            road_point(ctx, 0.34, offset + side * _ZONE_HALF_DEPTH),
        ]
        if all(0.03 <= x <= 0.97 and 0.03 <= y <= 0.97 for x, y in corners):
            return {
                "name": f"{ctx.name.split(' ', 1)[-1]} — restricted compound",
                "kind": "restricted",
                "polygon": [list(c) for c in corners],
                "direction_deg": None,
                "tolerance_deg": 75.0,
                "classes": [],          # empty = every class
                "params": {},
                "_centre": road_point(ctx, 0.5, offset),
                "_approach": road_point(ctx, 0.5, side * (ctx.half_w + 0.02)),
            }
    return None


# Starter tag vocabulary. Tags are rows, not an enum, per Model 1's handover:
# the first department that sends something new must not break the service.
SEED_TAGS: list[tuple[str, str, str]] = [
    ("traffic-violation", "#f97316", "category"),
    ("suspicious-behaviour", "#ef4444", "category"),
    ("vehicle-of-interest", "#8b5cf6", "category"),
    ("unattended-baggage", "#eab308", "category"),
    ("needs-review", "#3b82f6", "workflow"),
    ("escalated", "#dc2626", "workflow"),
    ("false-alarm", "#64748b", "workflow"),
    ("verified", "#22c55e", "workflow"),
    ("vip-movement", "#0ea5e9", "category"),
    ("hit-and-run", "#b91c1c", "category"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Scenario construction
# ═══════════════════════════════════════════════════════════════════════════
# Sampling rates: passes are emitted at --fps (default 2), stationary behaviours
# at a quarter of that. The live pipeline samples every camera uniformly; a
# parked object simply does not need 2 samples per second to be described, and
# emitting them would quadruple the database for no extra signal. Every
# threshold that decides an event is expressed in *seconds*; the frame-count
# thresholds (12 for loitering, 20 for stopped) are still met several times over.

def pick(weighted: list[tuple[str, float]], rng: random.Random) -> str:
    return rng.choices([v for v, _ in weighted], weights=[w for _, w in weighted])[0]


VEHICLE_MIX = [(c, w) for c, w in CLASS_MIX if c not in ("person",)]


@dataclass
class Scenario:
    detections: list[SynthDetection] = field(default_factory=list)
    truths: list[SynthTrack] = field(default_factory=list)
    zones: list[tuple[str, dict]] = field(default_factory=list)
    expectations: list[dict] = field(default_factory=list)
    journeys: list[dict] = field(default_factory=list)


class DemoBuilder:
    def __init__(self, ctxs: dict[str, CameraCtx], ef: EmbeddingFactory,
                 rng: random.Random, session_start: datetime, hours: float, fps: float):
        self.ctxs = ctxs
        self.ef = ef
        self.rng = rng
        self.session_start = session_start
        self.span_s = hours * 3600.0
        self.fps = fps
        self.slow_fps = max(0.4, fps / 4.0)
        self.sc = Scenario()

    # ── plumbing ──────────────────────────────────────────────────────────
    def _emit(self, ctx: CameraCtx, samples: list[Sample], cls: str, color: str,
              plate: Optional[str], sighting_vec, **kw) -> SynthTrack:
        dets, truth = emit_track(ctx, samples, cls, color, plate, sighting_vec,
                                 self.ef, self.rng, self.session_start, **kw)
        self.sc.detections.extend(dets)
        if dets:            # every crop was below the pipeline's size floor
            self.sc.truths.append(truth)
        return truth

    def _identity(self, cls: str, color: str):
        return self.ef.identity(cls, color, self.rng)

    def _expect(self, kind: str, camera: str, detail: str) -> None:
        """Record what this scenario was built to make fire.

        The point of writing it down is that the rule engine runs afterwards and
        independently: if it finds seven of the eight, the eighth is a real
        finding rather than something to be shrugged at.
        """
        self.sc.expectations.append({"event_type": kind, "camera": camera, "detail": detail})

    def _lane_lateral(self, ctx: CameraCtx, forward: bool) -> float:
        s = 1.0 if forward else -1.0
        return s * ctx.half_w * self.rng.uniform(LANE_MIN, LANE_MAX)

    def _footpath_lateral(self, ctx: CameraCtx) -> float:
        s = self.rng.choice([1.0, -1.0])
        return s * ctx.half_w * self.rng.uniform(*FOOTPATH)

    # ── background traffic ────────────────────────────────────────────────
    def background(self, ctx: CameraCtx, n: int) -> None:
        for _ in range(n):
            cls = pick(CLASS_MIX, self.rng)
            color = pick(COLOR_BY_CLASS[cls], self.rng)
            t0 = self.rng.uniform(0, max(1.0, self.span_s - 20))
            dur = self.rng.uniform(4.0, 9.0)
            forward = self.rng.random() < 0.55
            if cls == "person":
                lat = self._footpath_lateral(ctx)
                dur = self.rng.uniform(8.0, 22.0)
            else:
                lat = self._lane_lateral(ctx, forward)
            samples = traj_transit(ctx, t0, dur, self.fps, self.rng,
                                   reverse=not forward, lateral=lat)
            plate = make_plate(self.rng) if cls in PLATED_CLASSES else None
            ident = self._identity(cls, color)
            self._emit(ctx, samples, cls, color, plate,
                       self.ef.sighting(ident, self.rng))

    # ── multi-camera journeys ─────────────────────────────────────────────
    def journeys(self, n: int) -> None:
        """Vehicles that cross several cameras — the substrate the camera
        transition graph is learned from, and the only reason cross-camera
        investigation has anything to find."""
        for j in range(n):
            name, seq, transits = self.rng.choice(CORRIDORS)
            reverse = self.rng.random() < 0.5
            cams = list(reversed(seq)) if reverse else list(seq)
            hops = list(reversed(transits)) if reverse else list(transits)
            start = self.rng.randrange(0, max(1, len(cams) - 1))
            length = self.rng.randint(2, min(5, len(cams) - start))
            cls = pick(VEHICLE_MIX, self.rng)
            color = pick(COLOR_BY_CLASS[cls], self.rng)
            plate = make_plate(self.rng)
            ident = self._identity(cls, color)
            ref = f"J{j:03d}"
            t = self.rng.uniform(0, max(1.0, self.span_s * 0.75))
            seen: list[dict] = []
            for k in range(start, start + length):
                cam_id = cams[k]
                ctx = self.ctxs.get(cam_id)
                if ctx is None or t > self.span_s - 20:
                    break
                # A real corridor drops sightings: occlusion, a lorry alongside,
                # a plate out of the detector's reach. A graph builder that only
                # ever sees complete chains is not being tested.
                if self.rng.random() > 0.12:
                    dur = self.rng.uniform(5.0, 9.0)
                    truth = self._emit(
                        ctx,
                        traj_transit(ctx, t, dur, self.fps, self.rng,
                                     reverse=reverse,
                                     lateral=self._lane_lateral(ctx, not reverse)),
                        cls, color, plate, self.ef.sighting(ident, self.rng),
                        journey=ref, behaviour="journey")
                    seen.append({"camera": cam_id, "track_id": truth.track_id,
                                 "pts_s": round(t, 1), "reads": len(truth.reads)})
                if k < len(hops):
                    t += hops[k] * self.rng.uniform(0.82, 1.24)
            if len(seen) >= 2:
                self.sc.journeys.append({
                    "ref": ref, "corridor": name, "reverse": reverse,
                    "class": cls, "color": color, "plate": plate,
                    "sightings": seen,
                })

    # ── scripted incidents ────────────────────────────────────────────────
    # Each of these is built against a specific detector in ml/analytics/rules.py
    # and sized off that detector's own thresholds, so the rule engine has
    # something true to find. They are also written down in `expectations`, which
    # turns "the demo has events" into a pass/fail check.

    def loiterer(self, ctx: CameraCtx) -> None:
        """A person pacing on the footpath for well over loiter_min_dwell_s (120).

        Pacing, not standing: max_spread_norm has to clear 0.10 or the rule reads
        it as a stopped object and leaves it to the stopped-vehicle detector.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.6))
        x, y = road_point(ctx, self.rng.uniform(0.45, 0.65),
                          self.rng.choice([1.0, -1.0]) * ctx.half_w * 1.45)
        color = pick(COLOR_BY_CLASS["person"], self.rng)
        ident = self._identity("person", color)
        self._emit(ctx, traj_wander(x, y, 0.055, t0, 235.0, self.slow_fps, self.rng),
                   "person", color, None, self.ef.sighting(ident, self.rng),
                   behaviour="loiter")
        self._expect("loitering", ctx.external_id, "person pacing ~235 s on the footpath")

    def stopped_vehicle(self, ctx: CameraCtx) -> None:
        """A vehicle halted in-lane for longer than stopped_min_dwell_s (90).

        Placed near the camera so the plate is legible: a stopped vehicle with a
        consensus plate is the case an operator most wants, and it exercises ANPR
        and the rule engine on the same track.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.55))
        cls = pick([("car", 0.6), ("truck", 0.25), ("auto_rickshaw", 0.15)], self.rng)
        color = pick(COLOR_BY_CLASS[cls], self.rng)
        x, y = road_point(ctx, 0.70, self._lane_lateral(ctx, True))
        plate = make_plate(self.rng)
        ident = self._identity(cls, color)
        self._emit(ctx, traj_static(x, y, t0, 260.0, self.slow_fps, 0.005, self.rng),
                   cls, color, plate, self.ef.sighting(ident, self.rng),
                   behaviour="stopped", plate_read_rate=0.8)
        self._expect("stopped_vehicle", ctx.external_id,
                     f"{color} {cls} halted ~260 s in-lane, plate {plate}")

    def wrong_way(self, ctx: CameraCtx, zone_name: str) -> None:
        """A vehicle travelling against the flow *inside the direction zone*.

        It must be in the forward lane — the zone only covers that lane — while
        heading backwards along the axis. Legal traffic in the same lane heads
        along the axis; legal opposing traffic is in the other lane and outside
        the polygon. That is what makes this a real test rather than a guaranteed
        hit.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.7))
        cls = pick([("motorcycle", 0.55), ("car", 0.3), ("auto_rickshaw", 0.15)], self.rng)
        color = pick(COLOR_BY_CLASS[cls], self.rng)
        plate = make_plate(self.rng)
        ident = self._identity(cls, color)
        self._emit(ctx, traj_transit(ctx, t0, 8.0, self.fps, self.rng, reverse=True,
                                     lateral=ctx.half_w * 0.62),
                   cls, color, plate, self.ef.sighting(ident, self.rng),
                   behaviour="wrong_way")
        self._expect("wrong_way", ctx.external_id,
                     f"{color} {cls} against flow inside zone '{zone_name}'")

    def intruder(self, ctx: CameraCtx, zone: dict, zone_name: str) -> None:
        """A person who walks into a restricted polygon and stays.

        zone_intrusion needs points inside on at least 3 samples and a dwell of
        4 s; the walk-in is emitted too so the timeline shows an approach rather
        than a figure materialising inside the fence.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.65))
        color = pick(COLOR_BY_CLASS["person"], self.rng)
        ident = self._identity("person", color)
        samples = traj_approach_then_dwell(zone["_approach"], zone["_centre"], t0,
                                           9.0, 55.0, self.slow_fps, 0.010, self.rng)
        self._emit(ctx, samples, "person", color, None,
                   self.ef.sighting(ident, self.rng), behaviour="intrusion")
        self._expect("zone_intrusion", ctx.external_id,
                     f"person enters restricted zone '{zone_name}' and dwells ~55 s")

    def crowd(self, ctx: CameraCtx, n: int = 16) -> None:
        """*n* distinct person tracks in one 80 s window.

        Held under loiter_min_dwell_s on purpose: a crowd that also fired 16
        loitering events would bury the crowd event under its own members.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.5))
        cx, cy = road_point(ctx, 0.55, self.rng.choice([1.0, -1.0]) * (ctx.half_w + 0.10))
        for _ in range(n):
            px = min(0.95, max(0.05, cx + self.rng.gauss(0, 0.075)))
            py = min(0.95, max(0.20, cy + self.rng.gauss(0, 0.045)))
            color = pick(COLOR_BY_CLASS["person"], self.rng)
            ident = self._identity("person", color)
            self._emit(ctx, traj_wander(px, py, 0.03,
                                        t0 + self.rng.uniform(0, 12.0), 80.0,
                                        self.slow_fps, self.rng),
                       "person", color, None, self.ef.sighting(ident, self.rng),
                       behaviour="crowd")
        self._expect("crowd", ctx.external_id, f"{n} person tracks inside one 80 s window")

    def abandoned_object(self, ctx: CameraCtx, attended: bool) -> bool:
        """A bag left beside the road, clear of the pedestrian flow.

        Two variants, and the *attended* one is the more interesting: an object
        with a person standing beside it is not abandoned, and the rule's
        person-clearance test should suppress it. A demo that only shows the
        positive case proves the rule fires, not that it discriminates.

        In the unattended case a person is still emitted — someone puts the bag
        down and leaves — but their track ends before the object's begins, which
        is both what the detector actually sees (an object is only tracked once
        it is static) and why the clearance test does not veto it.

        The lateral offset comes from ``clear_lateral()`` rather than a chosen
        multiple of ``half_w``. Placed inside the footpath band at 1.6·half_w the
        unattended case never fired: background pedestrians pass within
        ``abandoned_person_clearance_norm`` of the bag all day, so the rule
        suppressed it — correctly. The scenario was wrong, not the threshold, and
        lowering the threshold to make the demo work would have been tuning the
        test to the answer.

        Returns False if no side of this camera has room for the object inside
        the frame, so the caller can try another camera instead of silently
        emitting nothing.
        """
        t0 = self.rng.uniform(60, max(120.0, self.span_s * 0.5))
        lateral = clear_lateral(ctx.half_w)
        placed: Optional[tuple[float, float, float]] = None
        for side in self.rng.sample([1.0, -1.0], 2):
            ox, oy = road_point(ctx, 0.60, side * lateral)
            ax, ay = road_point(ctx, 0.30, side * lateral)
            if all(0.04 <= v <= 0.96 for v in (ox, oy, ax, ay)):
                placed = (side, ox, oy)
                break
        if placed is None:
            return False
        side, ox, oy = placed
        obj = pick([("suitcase", 0.5), ("backpack", 0.3), ("handbag", 0.2)], self.rng)
        ident = self._identity(obj, "black")

        if attended:
            owner_color = pick(COLOR_BY_CLASS["person"], self.rng)
            owner_ident = self._identity("person", owner_color)
            self._emit(ctx, traj_wander(ox + 0.035, oy, 0.02, t0 - 20.0, 460.0,
                                        self.slow_fps, self.rng),
                       "person", owner_color, None,
                       self.ef.sighting(owner_ident, self.rng), behaviour="owner")
            self._emit(ctx, traj_static(ox, oy, t0, 430.0, self.slow_fps, 0.003, self.rng),
                       obj, "black", None, self.ef.sighting(ident, self.rng),
                       behaviour="attended_object")
            self._expect("abandoned_object:suppressed", ctx.external_id,
                         f"{obj} with owner within 0.035 — must NOT fire")
            return True

        dropper_color = pick(COLOR_BY_CLASS["person"], self.rng)
        dropper_ident = self._identity("person", dropper_color)
        approach = road_point(ctx, 0.30, side * lateral)
        self._emit(ctx, traj_approach_then_dwell(approach, (ox, oy), t0 - 60.0,
                                                 25.0, 14.0, self.slow_fps, 0.012, self.rng),
                   "person", dropper_color, None,
                   self.ef.sighting(dropper_ident, self.rng), behaviour="dropper")
        self._emit(ctx, traj_static(ox, oy, t0, 430.0, self.slow_fps, 0.003, self.rng),
                   obj, "black", None, self.ef.sighting(ident, self.rng),
                   behaviour="abandoned_object")
        self._expect("abandoned_object", ctx.external_id,
                     f"{obj} static ~430 s, nobody within {_CLEARANCE} during its window")
        return True

    def accident(self, ctx: CameraCtx) -> None:
        """Two vehicles converging on one point and stopping there.

        The detector's evidence is: overlapping boxes (IoU ≥ 0.15), real speed
        before contact (≥ 0.02/s), near-zero speed after (≤ 0.004/s), and both
        still present 8 s later. It is called accident_*candidate* because those
        four facts also describe a hard-braked near-miss, and the rule does not
        pretend otherwise.
        """
        t0 = self.rng.uniform(90, max(150.0, self.span_s * 0.6))
        meet = road_point(ctx, 0.66, ctx.half_w * 0.25)
        car_color = pick(COLOR_BY_CLASS["car"], self.rng)
        moto_color = pick(COLOR_BY_CLASS["motorcycle"], self.rng)
        car_plate, moto_plate = make_plate(self.rng), make_plate(self.rng)
        car_id = self._identity("car", car_color)
        moto_id = self._identity("motorcycle", moto_color)
        self._emit(ctx, traj_converge_and_stop(ctx, 0.26, ctx.half_w * 0.55, meet,
                                               t0, 6.0, 16.0, self.fps, self.rng),
                   "car", car_color, car_plate, self.ef.sighting(car_id, self.rng),
                   behaviour="accident", plate_read_rate=0.8)
        self._emit(ctx, traj_converge_and_stop(ctx, 0.95, -ctx.half_w * 0.55,
                                               (meet[0] + 0.012, meet[1]),
                                               t0 + 0.4, 6.0, 16.0, self.fps, self.rng),
                   "motorcycle", moto_color, moto_plate,
                   self.ef.sighting(moto_id, self.rng),
                   behaviour="accident", plate_read_rate=0.6)
        self._expect("accident_candidate", ctx.external_id,
                     f"{car_color} car + {moto_color} motorcycle converge and stop 16 s")

    def anpr_showcase(self, ctx: CameraCtx, n: int = 14) -> None:
        """Slow, close passes at a toll plaza — the case multi-frame ANPR is for.

        Long dwell in the near field means ~11 reads of the same plate at high
        nearness, which is where consensus should beat every individual read.
        """
        for _ in range(n):
            cls = pick([("car", 0.4), ("truck", 0.3), ("motorcycle", 0.2),
                        ("bus", 0.1)], self.rng)
            color = pick(COLOR_BY_CLASS[cls], self.rng)
            plate = make_plate(self.rng)
            ident = self._identity(cls, color)
            t0 = self.rng.uniform(0, max(1.0, self.span_s - 30))
            self._emit(ctx, traj_transit(ctx, t0, self.rng.uniform(10.0, 14.0),
                                         self.fps, self.rng,
                                         lateral=self._lane_lateral(ctx, True)),
                       cls, color, plate, self.ef.sighting(ident, self.rng),
                       behaviour="toll_pass", plate_read_rate=0.9)
        self._expect("anpr", ctx.external_id, f"{n} slow near-field passes for consensus")

    # ── assembly ──────────────────────────────────────────────────────────
    def build(self, density: int, journey_count: int, anpr_cam: str) -> Scenario:
        """Lay out the whole demo: zones first (incidents are placed relative to
        them), then background traffic, then journeys, then the scripted cast."""
        cams = sorted(self.ctxs)

        # Zones. Direction zones go on the busier road cameras; restricted zones
        # only where one fits off the carriageway and inside the frame.
        dir_cams = [c for c in cams if self.ctxs[c].kind == "road"][:12]
        for cam_id in dir_cams:
            ctx = self.ctxs[cam_id]
            self.sc.zones.append((cam_id, direction_zone(ctx)))
        restricted_placed: list[str] = []
        for cam_id in cams:
            if len(restricted_placed) >= 6:
                break
            z = restricted_zone(self.ctxs[cam_id])
            if z is not None:
                self.sc.zones.append((cam_id, z))
                restricted_placed.append(cam_id)

        zone_by_cam: dict[str, dict] = {}
        for cam_id, z in self.sc.zones:
            if z["kind"] == "restricted":
                zone_by_cam[cam_id] = z

        # Background traffic everywhere, scaled by camera role.
        for cam_id in cams:
            ctx = self.ctxs[cam_id]
            n = int(density * (1.6 if ctx.busy else 1.0) * self.span_s / 3600.0)
            self.background(ctx, max(3, n))

        self.journeys(journey_count)

        # ── the cast ──────────────────────────────────────────────────────
        # One scripted incident per camera. Two on the same camera makes a miss
        # unattributable — when the unattended bag shared cam17 with the 16-person
        # crowd, the abandoned-object rule was suppressed by the crowd and the
        # result looked like a rule failure rather than a scenario collision.
        taken: set[str] = set()

        def has(cam_id: str) -> bool:
            return cam_id in self.ctxs and cam_id not in taken

        for cam_id in ("cam06", "cam13"):
            if has(cam_id):
                self.loiterer(self.ctxs[cam_id])
                taken.add(cam_id)
        for cam_id in ("cam04", "cam09"):
            if has(cam_id):
                self.stopped_vehicle(self.ctxs[cam_id])
                taken.add(cam_id)
        # Wrong-way needs a direction zone to be inside, so the cameras are
        # chosen from the zones that were actually placed rather than named up
        # front — a hardcoded camera that turned out to be a junction would
        # silently produce no event.
        dir_placed = [(c, z) for c, z in self.sc.zones if z["kind"] == "direction"]
        for cam_id, z in [(c, z) for c, z in dir_placed if has(c)][:2]:
            self.wrong_way(self.ctxs[cam_id], z["name"])
            taken.add(cam_id)
        for cam_id in [c for c in zone_by_cam if has(c)][:2]:
            self.intruder(self.ctxs[cam_id], zone_by_cam[cam_id],
                          zone_by_cam[cam_id]["name"])
            taken.add(cam_id)
        for cam_id in ("cam17", "cam28"):
            if has(cam_id):
                self.crowd(self.ctxs[cam_id])
                taken.add(cam_id)
        for cam_id in ("cam14", "cam23"):
            if has(cam_id):
                self.accident(self.ctxs[cam_id])
                taken.add(cam_id)
        if has(anpr_cam):
            self.anpr_showcase(self.ctxs[anpr_cam])
            taken.add(anpr_cam)

        # The bags go last, on quiet cameras nothing else is using: the object
        # must stay clear of people for its whole window, and a restricted zone
        # on the same camera would put the scripted intruder right beside it —
        # besides making the bag itself an intrusion. Station cameras first,
        # since unattended baggage at a station is the realistic case anyway.
        quiet = [c for c in cams
                 if has(c) and c not in zone_by_cam
                 and self.ctxs[c].kind != "junction" and not self.ctxs[c].busy]
        quiet.sort(key=lambda c: (0 if "station" in self.ctxs[c].name.lower() else 1, c))
        for want_attended in (False, True):
            for cam_id in quiet:
                if cam_id in taken:
                    continue
                if self.abandoned_object(self.ctxs[cam_id], attended=want_attended):
                    taken.add(cam_id)
                    break

        self.sc.detections.sort(key=lambda d: d.ts)
        return self.sc


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════
# Everything this script writes is tagged so it can be found again:
#   cameras.metadata->>'seeded_by' = 'seed_demo'
#   detections.source              = 'synthetic'
#   detections.session_key         = 'demo-<seed>'
# --reset deletes on those markers and nothing else, which is why it can be run
# against a database that also holds real inference output.

SEEDED_BY = "seed_demo"


def upsert_cameras(conn, coords: dict[str, tuple[float, float]],
                   sentinel_base: str) -> dict[str, str]:
    """Insert or update the 30 Sentinel cameras. Returns {external_id: uuid}.

    ``ON CONFLICT (external_id) DO UPDATE`` mirrors scripts/seed.py so running
    both in either order converges instead of erroring, but note that it also
    stamps ``seeded_by`` on a camera seed.py created. That is deliberate: the
    camera rows are identical either way, and it means --reset cleans up after
    itself even if the real seeder ran first.
    """
    rows = []
    for cam_id, (lat, lng) in sorted(coords.items()):
        name, district, area, dept = CAMERA_META.get(
            cam_id, (cam_id, "Gujarat", "", "police"))
        dept_label, source_system, uid_code = DEPARTMENTS.get(
            dept, (dept, "Gujarat Police — Sentinel VMS", "POL"))
        # Model 1's join key. Its handover specifies GJ-<DEPT>-<6 digits>; the
        # digits come from the camera's own number so the same camera gets the
        # same uid on every run and the join is stable across re-seeds.
        camera_uid = f"GJ-{uid_code}-{int(cam_id.removeprefix('cam')):06d}"
        rows.append((
            cam_id, name, float(lat), float(lng),
            f"{sentinel_base}/{cam_id}/index.m3u8",
            district, area, dept, camera_uid,
            json.dumps({
                "seeded_by": SEEDED_BY,
                "district": district,
                "area": area,
                "department": dept,
                "department_label": dept_label,
                "source_system": source_system,
                "geocode_precision": "district",
                "stream_protocol": "hls",
            }),
        ))
    with conn.cursor() as cur:
        for r in rows:
            # district/area/department/camera_uid are real columns, not just
            # metadata keys — `camera_index` selects them by name and feature 4
            # filters on them, so writing them only into the JSONB left every
            # location filter matching nothing and every `district` in the camera
            # list NULL. The metadata copy stays as the record of what this
            # seeder wrote; the columns are what queries actually use.
            cur.execute("""
                INSERT INTO cameras (external_id, name, lat, lng, ingest_url, status,
                                     district, area, department, camera_uid,
                                     geocode_precision, metadata)
                VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s,
                        'district', %s::jsonb)
                ON CONFLICT (external_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    lat = EXCLUDED.lat,
                    lng = EXCLUDED.lng,
                    ingest_url = EXCLUDED.ingest_url,
                    district = COALESCE(EXCLUDED.district, cameras.district),
                    area = COALESCE(EXCLUDED.area, cameras.area),
                    department = COALESCE(EXCLUDED.department, cameras.department),
                    camera_uid = COALESCE(cameras.camera_uid, EXCLUDED.camera_uid),
                    geocode_precision = EXCLUDED.geocode_precision,
                    metadata = cameras.metadata || EXCLUDED.metadata
            """, r)
        cur.execute("SELECT external_id, id FROM cameras")
        return {r["external_id"]: str(r["id"]) for r in cur.fetchall()}


def upsert_tags(conn) -> int:
    """Seed the tag vocabulary.

    Migration 002 creates the ``tags`` table and inserts nothing, so event
    tagging has no vocabulary to tag with. These ten are the labels a control
    room actually needs; they are data, not enums, and an operator can add more.
    """
    with conn.cursor() as cur:
        for name, color, kind in SEED_TAGS:
            cur.execute("""
                INSERT INTO tags (name, color, kind)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    color = EXCLUDED.color, kind = EXCLUDED.kind
            """, (name, color, kind))
        cur.execute("SELECT count(*) AS n FROM tags")
        return int(cur.fetchone()["n"])


def upsert_zones(conn, zones: list[tuple[str, dict]], cam_uuid: dict[str, str]) -> int:
    """Write zone polygons.

    ``direction_deg``, ``tolerance_deg`` and ``classes`` are real columns —
    rules.py selects them by name — so they are mapped explicitly rather than
    swept into ``params``. Keys starting with '_' are builder scratch (the zone
    centre and the approach point the intruder walks in from) and never reach
    the database.

    There is no unique constraint on (camera_id, name), so idempotence comes from
    clearing this seeder's own zones first. Zones drawn by an operator have a
    different ``created_by`` and are left alone.
    """
    written = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM zones WHERE created_by = %s", (SEEDED_BY,))
        for cam_id, z in zones:
            cid = cam_uuid.get(cam_id)
            if not cid:
                continue
            params = {k: v for k, v in z.items()
                      if k not in ("name", "kind", "polygon", "direction_deg",
                                   "tolerance_deg", "classes", "params")
                      and not k.startswith("_")}
            params.update(z.get("params") or {})
            cur.execute("""
                INSERT INTO zones (camera_id, name, kind, polygon, direction_deg,
                                   tolerance_deg, params, classes, active, created_by)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, true, %s)
            """, (cid, z["name"], z["kind"], json.dumps(z["polygon"]),
                  z.get("direction_deg"), z.get("tolerance_deg"),
                  json.dumps(params), z.get("classes") or [], SEEDED_BY))
            written += 1
    return written


def insert_detections(conn, dets: list[SynthDetection], session_key: str,
                      batch: int = 2000) -> int:
    """Bulk-insert detections with execute_values.

    One row-at-a-time INSERT of 40k detections each carrying a 512-float vector
    takes minutes; batched it takes seconds. ``source='synthetic'`` is set here
    rather than defaulted, so it cannot be forgotten.
    """
    from psycopg2.extras import execute_values

    sql = """
        INSERT INTO detections
            (id, camera_id, track_id, class, bbox, confidence, plate_text,
             plate_confidence, embedding, thumbnail_key, ts, pts_ms,
             dominant_color, frame_w, frame_h, source, session_key)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(dets), batch):
            chunk = dets[i:i + batch]
            values = [(
                d.id, d.camera_id, d.track_id, d.class_name, json.dumps(d.bbox),
                d.confidence, d.plate_text, d.plate_confidence, d.embedding,
                None, d.ts, d.pts_ms, d.dominant_color, d.frame_w, d.frame_h,
                "synthetic", session_key,
            ) for d in chunk]
            execute_values(cur, sql, values, page_size=batch)
            total += len(chunk)
            conn.commit()
            log.info("  inserted %d/%d detections", total, len(dets))
    return total


def camera_kind(name: str) -> str:
    """Classify a camera from its name.

    Junctions matter because a *direction* zone cannot be placed on one: traffic
    legally turns at a junction, so a single permitted heading with a 70°
    tolerance would flag every legal right turn as wrong-way. Only cameras
    looking down a carriageway get one.
    """
    low = name.lower()
    if "tollnaka" in low or "toll" in low:
        return "toll"
    if any(k in low for k in ("char rasta", "teen rasta", "tran rasta", "circle",
                              "chowk", "rlvd", "bridge", "gate")):
        return "junction"
    return "road"


def build_contexts(coords: dict[str, tuple[float, float]],
                   cam_uuid: dict[str, str], rng: random.Random) -> dict[str, CameraCtx]:
    ctxs: dict[str, CameraCtx] = {}
    for cam_id in sorted(coords):
        cid = cam_uuid.get(cam_id)
        if not cid:
            continue
        name, _district, _area, dept = CAMERA_META.get(
            cam_id, (cam_id, "Gujarat", "", "police"))
        fw, fh = FRAME_SIZES[hash(cam_id) % len(FRAME_SIZES)]
        kind = camera_kind(name)
        ctx = CameraCtx(external_id=cam_id, uuid=cid, name=name,
                        frame_w=fw, frame_h=fh,
                        entry=(0.5, 0.2), exit=(0.5, 0.9), half_w=0.08,
                        kind=kind, busy=(kind in ("toll", "junction") or dept == "highways"))
        build_camera_geometry(ctx, rng)
        ctxs[cam_id] = ctx
    return ctxs


def reset_synthetic(conn, session_key: Optional[str] = None) -> dict[str, int]:
    """Delete this seeder's rows and nothing else.

    Scope markers, in the order the foreign keys demand:

    * ``events``   — reached through their ``tracks``, and through any synthetic
                     detection they point at (``track_uuid`` is ON DELETE SET
                     NULL, so deleting tracks first would orphan them silently
                     rather than remove them).
    * ``tracks``   — ``session_key LIKE 'demo-%%'``, or one exact key. The
                     session key is part of the table's UNIQUE constraint, so the
                     runner cannot lose it.
    * ``detections`` — ``source = 'synthetic'``.
    * ``zones``    — ``created_by = 'seed_demo'``. Operator-drawn zones survive.
    * ``camera_links`` — cleared wholesale: it is a derived cache the graph
                     builder rebuilds from observations, and links learned from
                     synthetic journeys are not worth keeping.

    ``cameras`` rows stay. They carry real coordinates and real stream URLs,
    scripts/seed.py writes the same rows, and deleting them would cascade away
    operator-drawn zones and every real detection attached to them.
    """
    track_pred = "session_key = %s" if session_key else "session_key LIKE 'demo-%%'"
    track_args = (session_key,) if session_key else ()
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"""DELETE FROM events
                 WHERE track_uuid IN (SELECT id FROM tracks WHERE {track_pred})
                    OR detection_id IN (SELECT id FROM detections
                                         WHERE source = 'synthetic')""",
            track_args)
        counts["events"] = cur.rowcount
        cur.execute(
            """DELETE FROM alerts
                WHERE detection_id IN (SELECT id FROM detections
                                        WHERE source = 'synthetic')""")
        counts["alerts"] = cur.rowcount
        cur.execute(f"DELETE FROM tracks WHERE {track_pred}", track_args)
        counts["tracks"] = cur.rowcount
        cur.execute("DELETE FROM detections WHERE source = 'synthetic'")
        counts["detections"] = cur.rowcount
        cur.execute("DELETE FROM zones WHERE created_by = %s", (SEEDED_BY,))
        counts["zones"] = cur.rowcount
        cur.execute("DELETE FROM camera_links")
        counts["camera_links"] = cur.rowcount
    conn.commit()
    return counts


# ═══════════════════════════════════════════════════════════════════════════
# Ground-truth report
# ═══════════════════════════════════════════════════════════════════════════
# The generator knows the true plate of every vehicle and every corrupted read
# it produced. The analytics layer does not. Writing that down turns multi-frame
# ANPR from a feature into a measurement: run the fold, join tracks.plate_text
# against this, and the recovery rate is a number rather than a claim.

def build_report(sc: Scenario, ctxs: dict[str, CameraCtx], session_key: str,
                 session_start: datetime, hours: float) -> dict:
    by_cam: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for d in sc.detections:
        by_class[d.class_name] = by_class.get(d.class_name, 0) + 1
    for t in sc.truths:
        by_cam[t.camera] = by_cam.get(t.camera, 0) + t.detections

    plated = [t for t in sc.truths if t.plate]
    multi = [t for t in plated if len(t.reads) >= 2]
    return {
        "session_key": session_key,
        "session_start": session_start.isoformat(),
        "hours": hours,
        "cameras": len(ctxs),
        "detections": len(sc.detections),
        "tracks_expected": len(sc.truths),
        "zones": len(sc.zones),
        "detections_by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "detections_by_camera": dict(sorted(by_cam.items())),
        "plated_tracks": len(plated),
        "tracks_with_multiple_reads": len(multi),
        "mean_reads_per_plated_track": round(
            sum(len(t.reads) for t in plated) / len(plated), 2) if plated else 0.0,
        "exact_single_read_rate": round(
            sum(1 for t in plated for r in t.reads if r == t.plate) /
            max(1, sum(len(t.reads) for t in plated)), 4),
        "expectations": sc.expectations,
        "journeys": sc.journeys,
        # The scoring key, filled by attach_plate_truth:
        #   {camera: {track_id: {"plate", "reads", …}}}
        "plate_truth": {},
    }


def attach_plate_truth(report: dict, sc: Scenario) -> None:
    truth = report["plate_truth"]
    for t in sc.truths:
        if not t.plate:
            continue
        truth.setdefault(t.camera, {})[str(t.track_id)] = {
            "plate": t.plate,
            "reads": t.reads,
            "behaviour": t.behaviour,
            "journey": t.journey,
            "class": t.class_name,
            "color": t.color,
        }


def print_summary(report: dict) -> None:
    r = report
    log.info("─" * 68)
    log.info("session_key      %s", r["session_key"])
    log.info("window           %s  (+%.1f h)", r["session_start"], r["hours"])
    log.info("cameras          %d", r["cameras"])
    log.info("zones            %d", r["zones"])
    log.info("detections       %d", r["detections"])
    log.info("tracks expected  %d", r["tracks_expected"])
    log.info("plated tracks    %d  (%d with ≥2 reads, mean %.1f reads)",
             r["plated_tracks"], r["tracks_with_multiple_reads"],
             r["mean_reads_per_plated_track"])
    log.info("single-read OCR accuracy  %.1f%%  ← what one frame gets right",
             100.0 * r["exact_single_read_rate"])
    log.info("multi-camera journeys     %d", len(r["journeys"]))
    log.info("─" * 68)
    log.info("Built to fire — the rule engine has not run yet:")
    for e in r["expectations"]:
        log.info("  %-28s %-8s %s", e["event_type"], e["camera"], e["detail"])
    log.info("─" * 68)
    log.info("Next:  python -m ml.analytics.runner --session-key %s -v",
             r["session_key"])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed the Setu database with synthetic detections, zones and tags.")
    ap.add_argument("--hours", type=float, default=4.0,
                    help="length of the simulated window (default 4)")
    ap.add_argument("--density", type=int, default=22,
                    help="background vehicles per camera per hour (default 22)")
    ap.add_argument("--journeys", type=int, default=60,
                    help="multi-camera journeys to generate (default 60)")
    ap.add_argument("--seed", type=int, default=7,
                    help="RNG seed; also names the session_key (default 7)")
    ap.add_argument("--anpr-camera", default="cam12",
                    help="camera to load with slow near-field plate passes")
    ap.add_argument("--reset", action="store_true",
                    help="delete existing synthetic rows first")
    ap.add_argument("--reset-only", action="store_true",
                    help="delete synthetic rows and exit")
    ap.add_argument("--report", type=Path,
                    help="write the ground-truth report as JSON to this path")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate and report, write nothing")
    ap.add_argument("--json", action="store_true",
                    help="print the report as JSON on stdout")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", stream=sys.stderr)

    session_key = f"demo-{args.seed}"
    rng = random.Random(args.seed)

    from ml.config import settings
    from ml.db import get_connection

    coords = load_camera_coords()
    log.info("loaded %d camera coordinates from scripts/seed.py", len(coords))

    conn = get_connection()
    try:
        if args.reset or args.reset_only:
            counts = reset_synthetic(conn)
            log.info("reset: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
            if args.reset_only:
                return 0

        sentinel_base = getattr(settings, "sentinel_base_url", None) or "https://cctv.corp8.cloud"
        cam_uuid = upsert_cameras(conn, coords, sentinel_base.rstrip("/"))
        conn.commit()
        log.info("cameras upserted: %d", len(cam_uuid))

        n_tags = upsert_tags(conn)
        conn.commit()
        log.info("tag vocabulary: %d rows", n_tags)

        ctxs = build_contexts(coords, cam_uuid, rng)

        # CLIP text encoder — the same ViT-B-32/openai weights ml.models loads, so
        # these vectors share a space with real image embeddings. Without that
        # parity the vectors are noise and every similarity search is a lie.
        ef = EmbeddingFactory(args.seed)
        all_classes = sorted({c for c, _ in CLASS_MIX} | OBJECT_CLASSES)
        all_colors = sorted({c for mix in COLOR_BY_CLASS.values() for c, _ in mix})
        log.info("encoding %d class × %d colour prompts with CLIP ViT-B-32…",
                 len(all_classes), len(all_colors))
        ef.load(all_classes, all_colors)

        session_start = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        builder = DemoBuilder(ctxs, ef, rng, session_start, args.hours, fps=2.0)
        sc = builder.build(args.density, args.journeys, args.anpr_camera)
        log.info("generated %d detections across %d tracks",
                 len(sc.detections), len(sc.truths))

        report = build_report(sc, ctxs, session_key, session_start, args.hours)
        attach_plate_truth(report, sc)

        if not args.dry_run:
            n_zones = upsert_zones(conn, sc.zones, cam_uuid)
            conn.commit()
            log.info("zones written: %d", n_zones)
            insert_detections(conn, sc.detections, session_key)
            log.info("detections written: %d", len(sc.detections))
        else:
            log.info("--dry-run: nothing written")
    finally:
        conn.close()

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, default=str))
        log.info("ground truth → %s", args.report)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
