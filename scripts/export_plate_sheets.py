"""Contact sheets of plate regions, sized so a human can actually read them.

The first ground-truth pass labelled nine plates, which is enough to see a shape
and not enough to defend a number. The bottleneck was legibility: whole-vehicle
crops shown many-to-a-page are unreadable, so most were marked unreadable when
the plate itself was fine.

This exports the *plate region* — the detector's box widened the same 25% the
pipeline uses — magnified to a fixed height and stacked a few per sheet. A plate
50 px wide becomes 900, which is the difference between guessing and reading.

The pipeline's own answer is deliberately not drawn on the image. Seeing the
machine's guess before writing your own is how a labelling set quietly acquires
the machine's bias, and the whole point of this set is to be independent of it.

Usage:
    python -m scripts.export_plate_sheets --limit 60 --per-sheet 8
    # read the sheets, fill artifacts/plate_truth_ext.json, then
    python -m scripts.score_plate_truth
"""
from __future__ import annotations

import argparse
import json
import os

import cv2
import numpy as np
import psycopg2
import psycopg2.extras
from minio import Minio

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
OUT_DIR = "artifacts/plate_sheets"
TRUTH = "artifacts/plate_truth_ext.json"

STRIP_W = 900          # magnified plate width — wide enough to read ten characters
LABEL_H = 34


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--per-sheet", type=int, default=8)
    ap.add_argument("--min-plate-width", type=int, default=45,
                    help="skip plates too small for a human to read either")
    args = ap.parse_args()

    from ml.models import get_plate_detector
    from ml.pipeline import _MAX_PLATE_ASPECT, _MIN_PLATE_ASPECT, _PLATE_BOX_PAD

    det = get_plate_detector()
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    cur = psycopg2.connect(DB).cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Ordered by read count so the sample leans toward tracks the pipeline had
    # something to work with. That biases upward and is stated rather than
    # hidden — it measures the reader where reading was possible at all.
    cur.execute(
        """SELECT t.id, t.plate_text, t.plate_confidence, t.plate_read_count,
                  t.thumbnail_key, c.external_id AS cam
           FROM tracks t JOIN cameras c ON c.id = t.camera_id
           WHERE t.plate_text IS NOT NULL AND t.thumbnail_key IS NOT NULL
           ORDER BY t.plate_read_count DESC NULLS LAST, t.plate_confidence DESC NULLS LAST
           LIMIT %s""",
        (args.limit,),
    )
    rows = cur.fetchall()
    os.makedirs(OUT_DIR, exist_ok=True)

    stub: dict[str, dict] = {}
    strips: list[np.ndarray] = []
    sheet = 0
    kept = 0

    def flush() -> None:
        nonlocal strips, sheet
        if not strips:
            return
        cv2.imwrite(os.path.join(OUT_DIR, f"plates_{sheet:02d}.png"), np.vstack(strips))
        strips = []
        sheet += 1

    for r in rows:
        try:
            raw = mc.get_object("setu-clips", r["thumbnail_key"]).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            continue
        if img is None:
            continue

        res = det(img, verbose=False, conf=0.25)[0]
        if res.boxes is None or len(res.boxes) == 0:
            continue
        bi = int(res.boxes.conf.argmax())
        x1, y1, x2, y2 = (int(v) for v in res.boxes.xyxy[bi])
        pw, ph = x2 - x1, y2 - y1
        vh, vw = img.shape[:2]
        if ph <= 0 or pw < args.min_plate_width:
            continue
        if not (_MIN_PLATE_ASPECT <= pw / ph <= _MAX_PLATE_ASPECT):
            continue

        dx, dy = int(pw * _PLATE_BOX_PAD), int(ph * _PLATE_BOX_PAD)
        crop = img[max(0, y1 - dy):min(vh, y2 + dy), max(0, x1 - dx):min(vw, x2 + dx)]
        if crop.size == 0:
            continue

        ch, cw = crop.shape[:2]
        strip = cv2.resize(crop, (STRIP_W, max(1, int(STRIP_W * ch / cw))),
                           interpolation=cv2.INTER_CUBIC)
        strip = cv2.copyMakeBorder(strip, 0, LABEL_H, 0, 0,
                                   cv2.BORDER_CONSTANT, value=(14, 14, 14))
        idx = f"{kept:03d}"
        cv2.putText(strip, f"#{idx}  {r['cam']}  {pw}x{ph}px",
                    (8, strip.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 2)
        strips.append(strip)

        stub[idx] = {
            # Anchor the label to the *crop*, not the track.
            #
            # A track id is not a stable identity for a plate. Splitting tracks
            # on time gaps rebuilt every group, and 11 of 17 labels then pointed
            # at a track that no longer held what it held when it was read —
            # ANPR "accuracy" fell from 67% to 40% without the reader changing
            # at all. The crop is what a human actually read, and it does not
            # move.
            "thumbnail_key": r["thumbnail_key"],
            "track_id": str(r["id"]),
            "camera": r["cam"],
            "predicted": r["plate_text"],
            "confidence": float(r["plate_confidence"] or 0),
            "reads": int(r["plate_read_count"] or 0),
            "plate_px": f"{pw}x{ph}",
            "truth": "",
            "legible": None,
        }
        kept += 1
        if len(strips) >= args.per_sheet:
            flush()
    flush()

    os.makedirs("artifacts", exist_ok=True)
    with open(TRUTH, "w", encoding="utf-8") as fh:
        json.dump(stub, fh, indent=2)

    print(f"  {kept} plate strips across {sheet} sheet(s) in {OUT_DIR}/")
    print(f"  template written to {TRUTH}")
    print("  fill in 'truth' and 'legible', then: python -m scripts.score_plate_truth")


if __name__ == "__main__":
    main()
