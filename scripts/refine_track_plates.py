"""Re-read each track's best crop, and prefer it over the vote when it earns it.

Why the vote stopped being the best answer
------------------------------------------
``ml/analytics/anpr.py`` folds every frame's read into one string by voting. That
is the right design for a weak OCR: its errors are close to random, they differ
per frame, and eleven bad reads genuinely do carry more information than the best
single one. Measured on the global model, the vote was worth having.

Swapping in the India-specific model inverted that. Measured on the same nine
hand-read plates:

    the track's best crop, read once      7/9  (78%)   CER 13.3%
    highest-confidence frame read         6/9  (67%)   CER 20.0%
    multi-frame consensus                 5/9  (56%)   CER 27.8%

The vote is now *losing* information. A strong model reading a clean crop is
already near-certain, and averaging that with its own weaker reads on blurrier
frames of the same vehicle drags it off. On track #016 the consensus turned a
correct ``GJ11CL8853`` into ``GJJ22T1846``.

So this reads ``tracks.thumbnail_key`` — the crop the fold already chose as the
track's best — and takes that answer when it is a legal plate the model is
confident about. Otherwise the consensus stands, because when every frame is poor
the vote is still the better estimator. Neither mechanism is discarded; they are
ordered by the evidence each actually has.

``plate_votes`` keeps the ballot either way, with ``refined_from`` recording
which path produced the final string, so the audit trail still explains the
number an operator is looking at.

Usage:
    python -m scripts.refine_track_plates --dry-run
    python -m scripts.refine_track_plates
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
from minio import Minio

from ml.plate_validator import validate_plate

log = logging.getLogger("setu.refine")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"

# A best-crop read has to clear both bars to displace the vote: it must be a
# legal Indian plate, and the model must be confident. Below this the crop is not
# obviously better evidence than the eleven frames that voted, so the vote keeps
# its answer.
MIN_CONF_TO_OVERRIDE = 0.55


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-conf", type=float, default=MIN_CONF_TO_OVERRIDE)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    from ml.models import get_plate_detector, get_plate_ocr
    from ml.pipeline import (
        _MAX_PLATE_ASPECT,
        _MAX_PLATE_HEIGHT_FRAC,
        _MAX_PLATE_WIDTH_FRAC,
        _MIN_PLATE_ASPECT,
        _MIN_PLATE_WIDTH,
        _MIN_READ_LEN,
        _PLATE_BOX_PAD,
    )

    det = get_plate_detector()
    ocr = get_plate_ocr()
    log.info("Refining with detector + %s", ocr.name)

    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    write = conn.cursor()

    sql = ("SELECT id, plate_text, plate_confidence, plate_read_count, plate_votes, "
           "thumbnail_key FROM tracks WHERE thumbnail_key IS NOT NULL "
           "AND plate_text IS NOT NULL ORDER BY plate_read_count DESC NULLS LAST")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    cur.execute(sql)
    tracks = cur.fetchall()
    log.info("%d tracks with a plate and a crop", len(tracks))

    examined = read_ok = replaced = kept = rejected = 0
    started = time.time()

    for t in tracks:
        examined += 1
        try:
            raw = mc.get_object("setu-clips", t["thumbnail_key"]).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            continue
        if img is None:
            continue

        r = det(img, verbose=False, conf=0.25)[0]
        if r.boxes is None or len(r.boxes) == 0:
            continue
        bi = int(r.boxes.conf.argmax())
        x1, y1, x2, y2 = (int(v) for v in r.boxes.xyxy[bi])
        pw, ph = x2 - x1, y2 - y1
        vh, vw = img.shape[:2]
        if ph <= 0 or pw < _MIN_PLATE_WIDTH:
            continue
        if not (_MIN_PLATE_ASPECT <= pw / ph <= _MAX_PLATE_ASPECT):
            continue
        if ph > vh * _MAX_PLATE_HEIGHT_FRAC or pw > vw * _MAX_PLATE_WIDTH_FRAC:
            continue

        dx, dy = int(pw * _PLATE_BOX_PAD), int(ph * _PLATE_BOX_PAD)
        crop = img[max(0, y1 - dy):min(vh, y2 + dy), max(0, x1 - dx):min(vw, x2 + dx)]
        if crop.size == 0:
            continue

        best = ocr.read(crop)
        if best is None or len(best.text) < _MIN_READ_LEN:
            continue
        read_ok += 1

        # Repair the read against the registration template before judging it.
        # This path used to test the raw string with ``matches_format``, which
        # is a pure predicate — so a crop read as ``GJ11C05760`` was thrown away
        # for having a five-digit tail, when the sixth character of a
        # ten-character plate is a letter and the answer was ``GJ11CO5760``. The
        # grammar was already written; this path simply never called it.
        repaired = validate_plate(best.text)
        if repaired is None:
            # Includes the case where the OCR said UNREADABLE.
            rejected += 1
            kept += 1
            continue

        # The vote keeps its answer unless the crop's read is confident.
        # Legality alone is not enough — a confidently wrong legal plate is the
        # most dangerous output this system can produce.
        if best.confidence < args.min_conf:
            kept += 1
            continue
        if repaired == t["plate_text"]:
            kept += 1
            continue

        votes = t["plate_votes"] or {}
        if isinstance(votes, str):
            votes = json.loads(votes)
        votes = dict(votes)
        votes.update({
            "refined_from": "best_crop",
            "consensus_was": t["plate_text"],
            "consensus_confidence": float(t["plate_confidence"] or 0),
            "best_crop_backend": best.backend,
            "best_crop_confidence": round(best.confidence, 4),
            "best_crop_raw": best.text,
            "best_crop_repaired": repaired != best.text,
        })

        if not args.dry_run:
            write.execute(
                "UPDATE tracks SET plate_text=%s, plate_confidence=%s, plate_votes=%s::jsonb "
                "WHERE id=%s",
                (repaired, best.confidence, json.dumps(votes), t["id"]),
            )
        replaced += 1

        if replaced % 25 == 0 and not args.dry_run:
            conn.commit()

    if not args.dry_run:
        conn.commit()

    print()
    print(f"{'':-<56}")
    print(f"  tracks examined          {examined}   in {(time.time()-started)/60:.1f} min")
    print(f"  best crop produced a read {read_ok}")
    print(f"  not a registration       {rejected}")
    print(f"  replaced the consensus   {replaced}")
    print(f"  consensus kept           {kept}")
    print(f"{'':-<56}")
    print("  'replaced' means the best crop gave a legal plate the model was")
    print(f"  at least {args.min_conf:.2f} confident of, and the vote disagreed.")


if __name__ == "__main__":
    main()
