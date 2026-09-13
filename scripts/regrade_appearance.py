"""Re-derive what a vehicle *looks like*, and refuse to guess when it cannot.

The complaint that started this
------------------------------
A black Maruti S-Cross at cam06, plate read correctly as GJ11BR2912, displayed
as a **white truck**. Not a near miss — wrong colour and wrong class, stated
with no qualification, on a screen an officer would act from.

Why it happened
---------------
``ml/pipeline.py`` classified both colour and class with CLIP zero-shot over the
*whole vehicle crop*, took the argmax, and stored it. Two things follow from
that and both are visible in this estate:

* **The crop is not the vehicle.** A front-on crop is mostly windscreen, and
  behind the windscreen is an interior. This car has an orange cloth on the
  dashboard, and CLIP scored the whole crop ``orange 34%``. Sampling the body
  below the glass gives ``black 47%``; removing the number plate — a bright
  white rectangle sitting in the middle of the body region — gives
  ``black 72%``.
* **argmax never abstains.** 61 of the 78 frames voted "white" for a black car,
  so the multi-frame vote faithfully averaged a classifier that was confidently
  wrong on every frame. A vote cannot rescue a systematic error, only a random
  one.

YOLO's own class was computed in the same loop and thrown away — the line read
``class_name=clip_class_name,  # CLIP zero-shot, not YOLO COCO label``, and
ruff had been reporting ``cls_id`` as an unused variable for as long as it
existed. On the full frame YOLO is a detector doing the job it was trained for;
CLIP zero-shot on a crop is a language model guessing.

What this script does, and what it tried and rejected
-----------------------------------------------------
**Re-deriving colour was tried and thrown away.** Sampling the bodywork fixes the
case that started this — GJ11BR2912 goes from ``white`` to ``black 72%`` — and
then fails on everything shaped differently. Ten crops where it disagreed with
the stored colour were read by eye: it was better on three, worse on five. It
called a yellow auto-rickshaw **black at 0.77 confidence** (below 55% height an
auto is dark undercarriage), a red bus white, and black motorcycles at night
silver. ``_BODY_TOP`` is a front-on-car assumption, and most of this estate is
not a front-on car. Replacing one unreliable estimate with another that is
*differently* unreliable is not an improvement, so colour is left alone and only
flagged.

**Class collapse is kept, because "vehicle" cannot be wrong.** YOLO's
frame-level class was never stored and cannot be recovered; YOLO re-run on a
tight crop is out of distribution and unreliable on its own. But two unreliable
estimates that *disagree* are together a reliable signal that the specific label
is not supported. On the eight crops read by eye it fired on all four errors and
none of the correct labels:

    CLIP truck / YOLO car   → vehicle   (a black S-Cross)
    CLIP truck / YOLO car   → vehicle   (a white Maruti)
    CLIP truck / YOLO bus   → vehicle   (a silver WagonR)
    CLIP auto_rickshaw / YOLO bus → vehicle  (an orange TATA lorry)

An officer is better served by "a vehicle" than by a confident "truck" that is a
car, and a colour marked uncertain is better than one presented as observed.

Usage:
    python -m scripts.regrade_appearance --dry-run
    python -m scripts.regrade_appearance
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import cv2
import numpy as np
import psycopg2
import psycopg2.extras
import torch
from minio import Minio

log = logging.getLogger("setu.appearance")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"

# The body starts below the glass. Measured on this estate's front-on crops: the
# windscreen occupies roughly the top half, and everything from 55% down is
# bonnet, grille, bumper and plate.
_BODY_TOP = 0.55

# A colour must win by this much to be recorded. Below it the crop does not
# support a colour and NULL is the honest answer — an operator filtering for
# "silver car" should not be handed a vehicle the system could not colour.
_MIN_TOP = 0.40
_MIN_MARGIN = 0.15

_COLOR_PROMPTS = [
    "a red coloured vehicle", "a blue coloured vehicle", "a white coloured vehicle",
    "a yellow coloured vehicle", "a black coloured vehicle",
    "a silver or grey coloured vehicle", "a green coloured vehicle",
    "an orange coloured vehicle",
]
_COLOR_NAMES = ["red", "blue", "white", "yellow", "black", "silver", "green", "orange"]

# COCO classes that mean "a vehicle of some kind" when YOLO and CLIP disagree
# only about which kind. car/truck/bus on an Indian road is a genuinely hard
# distinction — an SUV is a car to a person and a truck to COCO.
_VEHICLE_LIKE = {"car", "truck", "bus", "van", "auto_rickshaw"}


def body_region(img: np.ndarray, plate_box: tuple[int, int, int, int] | None) -> np.ndarray:
    """The painted bodywork: below the glass, with the number plate cut out."""
    h = img.shape[0]
    top = int(h * _BODY_TOP)
    body = img[top:, :].copy()
    if plate_box is not None and body.size:
        x1, y1, x2, y2 = plate_box
        yy1, yy2 = max(0, y1 - top), max(0, y2 - top)
        # Only remove it if something is left to measure.
        if yy2 > yy1 and (yy2 - yy1) < body.shape[0] - 8:
            body = np.delete(body, np.s_[yy1:yy2], axis=0)
    return body


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=14)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    import open_clip

    from ml.models import get_plate_detector, get_yolo

    yolo = get_yolo()
    plate_det = get_plate_detector()
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32-quickgelu", pretrained="openai")
    tokenizer = open_clip.get_tokenizer("ViT-B-32-quickgelu")
    model.eval()
    with torch.no_grad():
        text = model.encode_text(tokenizer(_COLOR_PROMPTS))
        text /= text.norm(dim=-1, keepdim=True)

    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    write = conn.cursor()

    sql = ("SELECT t.id, t.class, t.class_votes, t.dominant_color, t.color_votes, "
           "t.thumbnail_key, c.external_id AS cam "
           "FROM tracks t JOIN cameras c ON c.id = t.camera_id "
           "WHERE t.thumbnail_key IS NOT NULL ORDER BY t.detection_count DESC")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    cur.execute(sql)
    tracks = cur.fetchall()
    log.info("%d tracks with a crop", len(tracks))

    colour_changed = colour_cleared = colour_kept = 0
    class_contested = class_kept = 0
    examples: list[tuple] = []
    started = time.time()

    for i, t in enumerate(tracks, 1):
        if i % 250 == 0:
            log.info("  %d/%d  (%.1f min)", i, len(tracks), (time.time() - started) / 60)
        try:
            raw = mc.get_object("setu-clips", t["thumbnail_key"]).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            continue
        if img is None or img.size == 0:
            continue

        # ── Colour, from the bodywork ───────────────────────────────────────
        pb = None
        pr = plate_det(img, verbose=False, conf=0.25)[0]
        if pr.boxes is not None and len(pr.boxes):
            bi = int(pr.boxes.conf.argmax())
            pb = tuple(int(v) for v in pr.boxes.xyxy[bi])  # type: ignore[assignment]
        body = body_region(img, pb)

        new_colour = None
        top = margin = 0.0
        if body.size:
            from PIL import Image
            pil = Image.fromarray(cv2.cvtColor(body, cv2.COLOR_BGR2RGB))
            with torch.no_grad():
                emb = model.encode_image(preprocess(pil).unsqueeze(0))
                emb /= emb.norm(dim=-1, keepdim=True)
                probs = (100 * (emb @ text.T)[0]).softmax(-1)
            order = probs.argsort(descending=True)
            top = float(probs[order[0]])
            margin = top - float(probs[order[1]])
            if top >= _MIN_TOP and margin >= _MIN_MARGIN:
                new_colour = _COLOR_NAMES[int(order[0])]

        # The body-region reading is a second opinion, not a replacement — see
        # the module docstring for why replacing was tested and rejected. Where
        # the two disagree, the colour is kept and marked uncertain, and the
        # console shows it as an estimate rather than an observation.
        colour_contested = bool(new_colour) and new_colour != t["dominant_color"]

        # ── Class, by agreement between two independent methods ─────────────
        yr = yolo(img, verbose=False, conf=0.25)[0]
        yolo_class = None
        if yr.boxes is not None and len(yr.boxes):
            bi = int(yr.boxes.conf.argmax())
            yolo_class = yolo.names[int(yr.boxes.cls[bi])]

        stored_class = t["class"]
        new_class = stored_class
        contested = False
        if (yolo_class and stored_class and yolo_class != stored_class
                and yolo_class in _VEHICLE_LIKE and stored_class in _VEHICLE_LIKE):
            new_class = "vehicle"
            contested = True

        cv_ = dict(t["class_votes"] or {})
        col_ = dict(t["color_votes"] or {})
        if contested:
            cv_["contested"] = {"clip": stored_class, "yolo_on_crop": yolo_class}
            class_contested += 1
        else:
            class_kept += 1
        if colour_contested:
            col_["contested"] = {"whole_crop": t["dominant_color"],
                                 "bodywork": new_colour,
                                 "top": round(top, 3), "margin": round(margin, 3)}
            colour_changed += 1
        elif new_colour is None:
            colour_cleared += 1
        else:
            colour_kept += 1

        if (colour_contested or contested) and len(examples) < args.show:
            shown = f"?{t['dominant_color']}" if colour_contested else str(t["dominant_color"])
            examples.append((t["cam"], f"{t['dominant_color']} {stored_class}",
                             f"{shown} {new_class}", round(top, 2), round(margin, 2)))

        if not args.dry_run:
            # dominant_color is deliberately NOT overwritten.
            write.execute(
                "UPDATE tracks SET color_votes=%s::jsonb, class=%s, class_votes=%s::jsonb "
                "WHERE id=%s",
                (json.dumps(col_), new_class, json.dumps(cv_), t["id"]))
            if i % 100 == 0:
                conn.commit()

    if not args.dry_run:
        conn.commit()

    n = len(tracks)
    print()
    print(f"{'':=<72}")
    print("  Appearance re-derived" + ("   [DRY RUN]" if args.dry_run else ""))
    print(f"{'':=<72}")
    print(f"  tracks examined        {n}   in {(time.time()-started)/60:.1f} min")
    print(f"  colour confirmed       {colour_kept}   (bodywork agreed)")
    print(f"  colour marked uncertain {colour_changed}"
          f"   (bodywork disagreed — kept, flagged, never overwritten)")
    print(f"  colour unjudged        {colour_cleared}"
          f"   (bodywork had no opinion above {_MIN_MARGIN:.0%})")
    print(f"  class kept             {class_kept}")
    print(f"  class → 'vehicle'      {class_contested}"
          f"   (CLIP and YOLO named different vehicle types)")
    if examples:
        print(f"\n  {'cam':<8}{'was':<24}{'now':<24}{'top':>6}{'margin':>8}")
        for cam, was, now, tp, mg in examples:
            print(f"  {cam:<8}{was:<24}{now:<24}{tp:>6}{mg:>8}")


if __name__ == "__main__":
    main()
