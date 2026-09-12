"""Run the real pipeline's plate path over the restored crops.

Not a re-implementation — this constructs ``DetectionPipeline`` and calls the
same ``_extract_plate`` the worker calls, so what it measures is what production
does. Crops come from MinIO, which already holds the GPU run's output, so this
needs no camera and no re-indexing.

It prints the gate histogram, because "ANPR found nothing" has several different
causes and they have different fixes:

    no_box / not_plate_shaped   the plate detector
    too_small                   where the camera is pointed
    too_short / error           the OCR itself

Usage:
    python -m scripts.verify_anpr_fix --cams cam09,cam12,cam06,cam03 --limit 120
"""
from __future__ import annotations

import argparse

import cv2
import numpy as np
import psycopg2
from minio import Minio

from ml.plate_validator import format_typicality, is_probably_truncated, matches_format

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="cam09,cam12,cam06,cam03")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--min-width", type=int, default=120,
                    help="only vehicles at least this wide, in pixels")
    args = ap.parse_args()

    from ml.pipeline import DetectionPipeline

    pipe = DetectionPipeline()

    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """SELECT c.external_id, d.thumbnail_key, d.plate_text,
                  (d.bbox->>'x2')::float - (d.bbox->>'x1')::float AS w
           FROM detections d JOIN cameras c ON c.id = d.camera_id
           WHERE c.external_id = ANY(%s)
             AND d.class IN ('car','truck','bus','auto_rickshaw')
             AND (d.bbox->>'x2')::float - (d.bbox->>'x1')::float >= %s
           ORDER BY (d.bbox->>'x2')::float - (d.bbox->>'x1')::float DESC
           LIMIT %s""",
        (args.cams.split(","), args.min_width, args.limit),
    )
    rows = cur.fetchall()
    print(f"{len(rows)} vehicle crops at least {args.min_width}px wide\n")

    hits = []
    for cam, key, old, w in rows:
        raw = mc.get_object("setu-clips", key).read()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        text, conf = pipe._extract_plate(img)
        if text:
            hits.append((cam, int(w), old, text, conf))

    s = pipe.plate_report()
    print("where plate reads were lost")
    print("-" * 46)
    order = ["seen", "no_box", "low_conf", "not_plate_shaped", "bad_box",
             "too_small", "no_read", "too_short", "osd_reject", "error", "read", "valid"]
    for k in order:
        if k in s:
            print(f"  {k:<20} {s[k]:>6}")

    print()
    print(f"{'cam':<7}{'veh':>5}  {'was':<12}{'now':<12}{'conf':>6}  {'legal':<7}{'typical':>8}  note")
    print("-" * 78)
    legal_n = trunc_n = 0
    for cam, w, old, text, conf in hits[:40]:
        legal = matches_format(text)
        typ = format_typicality(text)
        note = ""
        if legal:
            legal_n += 1
            if is_probably_truncated(text):
                trunc_n += 1
                note = "partial — short tail"
        print(f"{cam:<7}{w:>5}  {(old or '—'):<12}{text:<12}{(conf or 0):>6.2f}  "
              f"{('yes' if legal else 'no'):<7}{typ:>8.2f}  {note}")

    print("-" * 78)
    print(f"reads returned {len(hits)}   legal plates {legal_n}   "
          f"of those flagged partial {trunc_n}")


if __name__ == "__main__":
    main()
