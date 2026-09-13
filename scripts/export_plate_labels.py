"""Export plate crops for human labelling, and score the pipeline against them.

Why this exists
---------------
Every ANPR number quoted so far is *format validity* — whether the string the
pipeline produced is a legal Indian plate. That is a useful proxy and it is not
accuracy. A read of ``GJ11AB1234`` when the vehicle is ``GJ11AB5678`` is perfectly
legal and perfectly wrong, and no amount of format checking will notice.

Accuracy needs ground truth, and ground truth for plates means a human reading
the crop. This does the two halves of that:

    export   writes enlarged crops as a numbered contact sheet, with the
             pipeline's own read deliberately *not* drawn on the image — seeing
             the machine's answer before writing your own is how a labelling set
             acquires the machine's bias.

    score    takes the filled-in truth file and reports exact-match accuracy,
             character error rate, and — the number that actually matters for an
             operator — whether confidence predicts correctness.

Usage:
    python -m scripts.export_plate_labels export --limit 40
    # ...fill in artifacts/plate_truth.json...
    python -m scripts.export_plate_labels score
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
TRUTH = "artifacts/plate_truth.json"
SHEET_DIR = "artifacts/plate_labels"


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j - 1] + (ca != cb), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def fetch(limit: int) -> list[dict]:
    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Ordered by read count so the sample leans toward tracks where the consensus
    # had something to work with. That biases *upward* and is stated rather than
    # hidden: it measures the vote where the vote applies, and a single-read
    # track is already capped at 0.28 confidence by design.
    cur.execute(
        """SELECT t.id, t.plate_text, t.plate_confidence, t.plate_read_count,
                  t.thumbnail_key, c.external_id AS cam
           FROM tracks t JOIN cameras c ON c.id = t.camera_id
           WHERE t.plate_text IS NOT NULL AND t.thumbnail_key IS NOT NULL
           ORDER BY t.plate_read_count DESC NULLS LAST, t.plate_confidence DESC NULLS LAST
           LIMIT %s""",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def do_export(limit: int, per_sheet: int) -> None:
    rows = fetch(limit)
    if not rows:
        raise SystemExit("No tracks with a plate. Run the backfill and the fold first.")

    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    os.makedirs(SHEET_DIR, exist_ok=True)

    stub: dict[str, dict] = {}
    tiles: list[np.ndarray] = []
    sheet = 0

    def flush() -> None:
        nonlocal tiles, sheet
        if not tiles:
            return
        width = max(t.shape[1] for t in tiles)
        padded = [
            cv2.copyMakeBorder(t, 0, 0, 0, width - t.shape[1],
                               cv2.BORDER_CONSTANT, value=(16, 16, 16))
            for t in tiles
        ]
        cv2.imwrite(os.path.join(SHEET_DIR, f"sheet_{sheet:02d}.jpg"), np.vstack(padded))
        tiles = []
        sheet += 1

    for i, r in enumerate(rows):
        try:
            raw = mc.get_object("setu-clips", r["thumbnail_key"]).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            continue
        if img is None:
            continue

        h, w = img.shape[:2]
        # Two views side by side: the whole vehicle, so the plate can be located
        # and read in context, and a magnified strip of the lower body where the
        # plate sits. The magnified half is what makes the characters legible at
        # these crop sizes; the whole-vehicle half is what stops a mis-framed
        # magnification from being labelled as if it were the plate.
        band = img[int(h * 0.45):, :]
        if band.size == 0:
            band = img
        bh, bw = band.shape[:2]
        zoom = cv2.resize(band, (760, max(1, int(bh * 760 / max(bw, 1)))),
                          interpolation=cv2.INTER_CUBIC)
        ctx = cv2.resize(img, (300, max(1, int(h * 300 / max(w, 1)))),
                         interpolation=cv2.INTER_CUBIC)

        rows_h = max(zoom.shape[0], ctx.shape[0])
        canvas = np.full((rows_h + 34, 760 + 300 + 8, 3), 16, np.uint8)
        canvas[:zoom.shape[0], :760] = zoom
        canvas[:ctx.shape[0], 768:768 + 300] = ctx
        # The index and camera only. The pipeline's read is withheld on purpose.
        cv2.putText(canvas, f"#{i:03d}  {r['cam']}", (8, rows_h + 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        tiles.append(canvas)

        stub[f"{i:03d}"] = {
            "track_id": str(r["id"]),
            "camera": r["cam"],
            "predicted": r["plate_text"],
            "confidence": float(r["plate_confidence"] or 0),
            "reads": int(r["plate_read_count"] or 0),
            "truth": "",          # <- fill in: the plate as read by eye
            "legible": None,      # <- true/false: could a human read it at all?
        }
        if len(tiles) >= per_sheet:
            flush()
    flush()

    os.makedirs("artifacts", exist_ok=True)
    with open(TRUTH, "w", encoding="utf-8") as fh:
        json.dump(stub, fh, indent=2)

    print(f"  {len(stub)} crops across {sheet} sheet(s) in {SHEET_DIR}/")
    print(f"  truth template written to {TRUTH}")
    print("  fill in 'truth' (uppercase, no spaces) and 'legible', then:")
    print("    python -m scripts.export_plate_labels score")


def do_score() -> None:
    if not os.path.exists(TRUTH):
        raise SystemExit(f"No {TRUTH}. Run the export step first.")
    with open(TRUTH, encoding="utf-8") as fh:
        data = json.load(fh)

    labelled = [v for v in data.values() if (v.get("truth") or "").strip()]
    if not labelled:
        raise SystemExit(f"No 'truth' values filled in yet in {TRUTH}.")

    legible = [v for v in labelled if v.get("legible") is not False]
    exact = sum(1 for v in legible if v["predicted"].upper() == v["truth"].upper().strip())
    chars = sum(levenshtein(v["predicted"].upper(), v["truth"].upper().strip()) for v in legible)
    total_chars = sum(len(v["truth"].strip()) for v in legible)

    print(f"{'':-<58}")
    print("  ANPR accuracy against human-read ground truth")
    print(f"{'':-<58}")
    print(f"  crops labelled           {len(labelled)}")
    print(f"  of those, legible by eye {len(legible)}")
    print(f"  exact match              {exact}/{len(legible)}"
          f"  ({100 * exact / max(len(legible), 1):.1f}%)")
    print(f"  character error rate     {100 * chars / max(total_chars, 1):.1f}%")

    # Calibration. A confidence number is only worth showing if it predicts
    # correctness — a model that is 80% confident should be right about 80% of
    # the time, and if it is not, the number is decoration.
    print()
    print("  calibration — does confidence predict correctness?")
    print(f"  {'confidence':<14}{'n':>4}{'exact':>7}{'accuracy':>10}")
    buckets = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)]
    for lo, hi in buckets:
        grp = [v for v in legible if lo <= v["confidence"] < hi]
        if not grp:
            continue
        ok = sum(1 for v in grp if v["predicted"].upper() == v["truth"].upper().strip())
        print(f"  {f'{lo:.1f}–{hi:.1f}':<14}{len(grp):>4}{ok:>7}{100 * ok / len(grp):>9.0f}%")

    print()
    print("  misses")
    for k, v in sorted(data.items()):
        t = (v.get("truth") or "").strip().upper()
        if not t or v.get("legible") is False:
            continue
        if v["predicted"].upper() != t:
            print(f"    #{k}  {v['camera']:<7} read {v['predicted']:<12} truth {t:<12}"
                  f"  conf {v['confidence']:.2f}  reads {v['reads']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["export", "score"])
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--per-sheet", type=int, default=8)
    args = ap.parse_args()
    if args.mode == "export":
        do_export(args.limit, args.per_sheet)
    else:
        do_score()


if __name__ == "__main__":
    main()
