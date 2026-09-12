"""Harvest a plate-detection training set from this estate's own crops.

The problem being solved
------------------------
The incumbent detectors miss the plate entirely on most crops — ``no_box`` was
93 of 150 on the chokepoint cameras and 280 of 300 estate-wide. That is a
*recall* failure, and it is the one failure naive pseudo-labelling cannot fix:
training a detector on the boxes it already produces teaches it only to keep
doing what it does.

So the labels come from the **union of several detectors run at a deliberately
low confidence**, filtered by a signal none of them can see — whether the region
actually reads as a licence plate. A box that detector A missed and detector B
found at 0.07 confidence, which then OCRs to ``GJ23HD9762``, is exactly the
example A needs and cannot generate for itself.

Why OCR is a sound label source here
------------------------------------
It is a genuinely independent check. The OCR was trained on plates, not on these
detectors' outputs, and an Indian plate has enough structure — two letters, two
digits, one or two letters, four digits, a valid state code — that a random patch
of bodywork reading as one is unlikely. Requiring a *legal* plate rather than any
string is what keeps precision high enough for the labels to be worth training on.

The cost is a known bias: regions the OCR cannot read are never labelled, so the
model learns to find legible plates. For this application that is the right bias
— an illegible plate is not a detection anyone can act on.

Output is standard YOLO format, ready for ultralytics:

    plate_dataset/
      images/train/*.jpg   labels/train/*.txt
      images/val/*.jpg     labels/val/*.txt
      data.yaml

Usage:
    python -m scripts.build_plate_dataset --limit 8000
    python -m scripts.build_plate_dataset --cams cam06,cam09,cam12,cam03 --limit 6000
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import random
import threading
import time

import cv2
import numpy as np
import psycopg2
from minio import Minio

from ml.plate_validator import format_typicality, matches_format

log = logging.getLogger("setu.dataset")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
VEHICLES = ("car", "truck", "bus", "motorcycle", "auto_rickshaw", "tractor")

DETECTORS = [
    ("Koushim/yolov8-license-plate-detection", "best.pt"),
    ("morsetechlab/yolov11-license-plate-detection", "license-plate-finetune-v1n.pt"),
    ("FarAwayFer/yolov8n_license_plate_recognition", "best_yolov8n.pt"),
]

# Deliberately far below anything production would accept — the point is to
# surface candidates the OCR can then adjudicate.
HARVEST_CONF = 0.05
MIN_ASPECT, MAX_ASPECT = 1.2, 7.0
MAX_H_FRAC, MAX_W_FRAC = 0.5, 0.9
MIN_PLATE_W = 32
BOX_PAD = 0.25
# Cap on OCR adjudications per crop — see the sort in main().
MAX_CANDIDATES_PER_CROP = 3
VAL_FRACTION = 0.15


def iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def fetch_worker(keys: queue.Queue, out: queue.Queue) -> None:
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    while True:
        try:
            det_id, cam, key = keys.get(timeout=1.0)
        except queue.Empty:
            return
        try:
            raw = mc.get_object("setu-clips", key).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        out.put((det_id, cam, img))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cams", default="")
    ap.add_argument("--limit", type=int, default=8000)
    ap.add_argument("--min-vehicle-width", type=int, default=90)
    ap.add_argument("--out", default="plate_dataset")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    random.seed(args.seed)

    from fast_plate_ocr import LicensePlateRecognizer
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO

    from ml.models import device

    dets = []
    for repo, fname in DETECTORS:
        try:
            dets.append(YOLO(hf_hub_download(repo, fname)).to(device))
            log.info("Loaded harvester %s", repo)
        except Exception as exc:
            log.warning("Skipping %s (%s)", repo, type(exc).__name__)
    ocr = LicensePlateRecognizer("cct-s-v2-global-model", device="cpu")
    colour = ocr.config.image_color_mode

    where = ["d.thumbnail_key IS NOT NULL", "d.class IN %s",
             "(d.bbox->>'x2')::float - (d.bbox->>'x1')::float >= %s"]
    params: list = [VEHICLES, args.min_vehicle_width]
    if args.cams:
        where.append("c.external_id = ANY(%s)")
        params.append(args.cams.split(","))
    cur = psycopg2.connect(DB).cursor()
    cur.execute(
        "SELECT d.id, c.external_id, d.thumbnail_key FROM detections d "
        "JOIN cameras c ON c.id = d.camera_id WHERE " + " AND ".join(where) +
        f" ORDER BY random() LIMIT {args.limit}",
        params,
    )
    rows = cur.fetchall()
    log.info("%d candidate crops", len(rows))

    for split in ("train", "val"):
        os.makedirs(os.path.join(args.out, "images", split), exist_ok=True)
        os.makedirs(os.path.join(args.out, "labels", split), exist_ok=True)

    key_q: queue.Queue = queue.Queue()
    img_q: queue.Queue = queue.Queue(maxsize=256)
    for r in rows:
        key_q.put(r)
    for _ in range(8):
        threading.Thread(target=fetch_worker, args=(key_q, img_q), daemon=True).start()

    kept = 0
    candidates = 0
    by_source = {"union-only": 0, "agreed": 0}
    started = time.time()

    for i in range(len(rows)):
        try:
            det_id, cam, img = img_q.get(timeout=120)
        except queue.Empty:
            break
        if img is None:
            continue
        vh, vw = img.shape[:2]

        # Every candidate box from every harvester, deduped by overlap.
        boxes: list[tuple[tuple, int]] = []
        for di, det in enumerate(dets):
            r = det(img, verbose=False, conf=HARVEST_CONF)[0]
            if r.boxes is None:
                continue
            for bb in r.boxes.xyxy:
                x1, y1, x2, y2 = (int(v) for v in bb)
                pw, ph = x2 - x1, y2 - y1
                if ph <= 0 or pw < MIN_PLATE_W:
                    continue
                if not (MIN_ASPECT <= pw / ph <= MAX_ASPECT):
                    continue
                if ph > vh * MAX_H_FRAC or pw > vw * MAX_W_FRAC:
                    continue
                merged = False
                for j, (eb, mask) in enumerate(boxes):
                    if iou((x1, y1, x2, y2), eb) > 0.5:
                        boxes[j] = (eb, mask | (1 << di))
                        merged = True
                        break
                if not merged:
                    boxes.append(((x1, y1, x2, y2), 1 << di))

        # Adjudicating a candidate costs an OCR pass, and at 0.05 confidence the
        # harvesters return a long tail of near-noise boxes. Keeping the best few
        # per crop is where the yield is: a crop with eight candidates does not
        # contain eight plates, it contains one plate and seven guesses.
        boxes.sort(key=lambda b: -((b[0][2] - b[0][0]) * (b[0][3] - b[0][1])))
        boxes = boxes[:MAX_CANDIDATES_PER_CROP]
        candidates += len(boxes)

        # Adjudicate each candidate by whether it reads as a legal plate.
        accepted: list[tuple] = []
        for (x1, y1, x2, y2), mask in boxes:
            pw, ph = x2 - x1, y2 - y1
            dx, dy = int(pw * BOX_PAD), int(ph * BOX_PAD)
            crop = img[max(0, y1 - dy):min(vh, y2 + dy), max(0, x1 - dx):min(vw, x2 + dx)]
            if crop.size == 0:
                continue
            arr = (cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if colour == "grayscale"
                   else cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
            try:
                pred = ocr.run(arr, return_confidence=True)[0]
                txt = "".join(c for c in (pred.plate or "").upper() if c.isalnum())
            except Exception:
                continue
            if not matches_format(txt) or format_typicality(txt) < 0.2:
                continue
            accepted.append((x1, y1, x2, y2))
            by_source["agreed" if bin(mask).count("1") > 1 else "union-only"] += 1

        if not accepted:
            continue

        split = "val" if random.random() < VAL_FRACTION else "train"
        stem = f"{cam}_{det_id}"
        cv2.imwrite(os.path.join(args.out, "images", split, stem + ".jpg"), img)
        with open(os.path.join(args.out, "labels", split, stem + ".txt"), "w") as fh:
            for x1, y1, x2, y2 in accepted:
                cx, cy = (x1 + x2) / 2 / vw, (y1 + y2) / 2 / vh
                bw, bh = (x2 - x1) / vw, (y2 - y1) / vh
                fh.write(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
        kept += 1

        if (i + 1) % 500 == 0:
            rate = (i + 1) / max(time.time() - started, 1e-6)
            log.info("%d/%d  %.1f/s  labelled images=%d  boxes=%d",
                     i + 1, len(rows), rate, kept,
                     by_source["agreed"] + by_source["union-only"])

    with open(os.path.join(args.out, "data.yaml"), "w") as fh:
        fh.write(
            f"path: {os.path.abspath(args.out)}\n"
            "train: images/train\nval: images/val\n\nnc: 1\nnames: ['license_plate']\n"
        )

    total = by_source["agreed"] + by_source["union-only"]
    print()
    print(f"{'':-<58}")
    print(f"  crops examined        {len(rows)}")
    print(f"  candidate boxes       {candidates}")
    print(f"  accepted (OCR-legal)  {total}")
    print(f"    found by >1 model   {by_source['agreed']}")
    print(f"    found by only one   {by_source['union-only']}   <- the recall the incumbent lacks")
    print(f"  labelled images       {kept}")
    print(f"{'':-<58}")
    print(f"  dataset at {os.path.abspath(args.out)}")
    print("  next: python -m scripts.train_plate_detector")


if __name__ == "__main__":
    main()
