"""Export the plate regions the pipeline feeds its OCR, for head-to-head testing.

Why a separate export
---------------------
Comparing OCR models fairly means giving each the *same* input. The pipeline's
input is not the vehicle crop — it is the detector's plate box widened by 25%,
which is the padding that took full-length reads from 0 to 18 per 70 crops. A
candidate model evaluated on anything else is being asked a different question.

So this writes exactly that region to disk, named by the ground-truth index, so a
model with its own CLI (``Awiros/anpr-ocr`` ships one) can be pointed at the
directory and its output lined up against the plates a human read.

Usage:
    python -m scripts.export_plate_regions --out artifacts/plate_regions
"""
from __future__ import annotations

import argparse
import json
import os

import cv2
import numpy as np
import psycopg2
from minio import Minio

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
TRUTH = "artifacts/plate_truth.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="artifacts/plate_regions")
    ap.add_argument("--all-tracks", action="store_true",
                    help="export every track with a plate, not just the labelled ones")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    from ml.models import get_plate_detector
    from ml.pipeline import _MAX_PLATE_ASPECT, _MIN_PLATE_ASPECT, _PLATE_BOX_PAD

    det = get_plate_detector()
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    wanted: dict[str, str] = {}          # track_id -> output stem
    if args.all_tracks:
        cur.execute(
            "SELECT id FROM tracks WHERE plate_text IS NOT NULL "
            "ORDER BY plate_read_count DESC NULLS LAST LIMIT %s", (args.limit,))
        for (tid,) in cur.fetchall():
            wanted[str(tid)] = str(tid)
    else:
        with open(TRUTH, encoding="utf-8") as fh:
            truth = json.load(fh)
        for idx, row in truth.items():
            wanted[row["track_id"]] = idx

    os.makedirs(args.out, exist_ok=True)
    written = skipped = 0

    for track_id, stem in wanted.items():
        cur.execute(
            "SELECT thumbnail_key FROM tracks WHERE id = %s AND thumbnail_key IS NOT NULL",
            (track_id,),
        )
        row = cur.fetchone()
        if not row:
            skipped += 1
            continue
        try:
            raw = mc.get_object("setu-clips", row[0]).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            skipped += 1
            continue
        if img is None:
            skipped += 1
            continue

        r = det(img, verbose=False, conf=0.25)[0]
        if r.boxes is None or len(r.boxes) == 0:
            skipped += 1
            continue
        bi = int(r.boxes.conf.argmax())
        x1, y1, x2, y2 = (int(v) for v in r.boxes.xyxy[bi])
        pw, ph = x2 - x1, y2 - y1
        vh, vw = img.shape[:2]
        if ph <= 0 or pw < 40 or not (_MIN_PLATE_ASPECT <= pw / ph <= _MAX_PLATE_ASPECT):
            skipped += 1
            continue

        dx, dy = int(pw * _PLATE_BOX_PAD), int(ph * _PLATE_BOX_PAD)
        crop = img[max(0, y1 - dy):min(vh, y2 + dy), max(0, x1 - dx):min(vw, x2 + dx)]
        if crop.size == 0:
            skipped += 1
            continue

        cv2.imwrite(os.path.join(args.out, f"{stem}.png"), crop)
        written += 1

    print(f"  {written} plate regions written to {args.out}/")
    print(f"  {skipped} skipped (no crop, no plate box, or wrong shape)")
    print("\n  Point a candidate OCR at that directory, e.g.")
    print("    python artifacts/awiros/test.py \\")
    print(f"      --image_path {args.out} --output_json artifacts/awiros_reads.json")


if __name__ == "__main__":
    main()
