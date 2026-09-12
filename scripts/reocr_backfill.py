"""Re-read every stored crop with the fixed plate pipeline.

Why this rather than a re-index
-------------------------------
The GPU run already wrote 55,195 vehicle crops to MinIO, one per detection. The
frames they came from are gone, but the crops are exactly what ``_extract_plate``
consumes, so the plate stage can be re-run without fetching a single segment.
A full re-index would take hours and re-derive detections, tracks and embeddings
that are already correct; only the plate reads were wrong.

What changes
------------
* Plate reads come from the repaired path: 25% box padding, no CLAHE, the
  10-slot CCT model on the CPU provider, geometry gating, and the clock guard.
* **Person crops are cleared, not re-read.** 1,231 person detections currently
  carry a plate, which is not a marginal error — a person has no plate, so every
  one of those strings is noise that a track's consensus vote then has to
  outweigh. The old pipeline ran OCR on every detection deliberately, reasoning
  that CLIP might have relabelled a vehicle as a person; that trade was a bad
  one, and gating to vehicle classes is both faster and cleaner.

Afterwards run ``python -m ml.analytics.runner`` to fold the new reads into
track-level consensus — that is the stage this exists to feed.

Usage:
    python -m scripts.reocr_backfill --dry-run --limit 200
    python -m scripts.reocr_backfill                    # all vehicle crops
    python -m scripts.reocr_backfill --cams cam06,cam09 # a subset
"""
from __future__ import annotations

import argparse
import logging
import queue
import threading
import time

import cv2
import numpy as np
import psycopg2
import psycopg2.extras
from minio import Minio

from ml.plate_validator import is_probably_truncated, matches_format

log = logging.getLogger("setu.reocr")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle", "auto_rickshaw", "tractor")

# MinIO fetches are network-bound and inference is compute-bound, so the two are
# separated: a small pool of readers keeps a queue full while the main thread
# does nothing but run the models.
FETCH_THREADS = 8
QUEUE_DEPTH = 256


def fetch_worker(keys: queue.Queue, out: queue.Queue, stop: threading.Event) -> None:
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    while not stop.is_set():
        try:
            item = keys.get(timeout=1.0)
        except queue.Empty:
            return
        det_id, cam, key = item
        try:
            raw = mc.get_object("setu-clips", key).read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        out.put((det_id, cam, img))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cams", default="", help="comma-separated camera ids; default all")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
    ap.add_argument("--batch", type=int, default=500, help="rows per DB commit")
    ap.add_argument("--skip-person-clear", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    from ml.pipeline import DetectionPipeline

    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    # ── Clear plate reads on non-vehicles ────────────────────────────────────
    if not args.skip_person_clear and not args.dry_run:
        cur.execute(
            "UPDATE detections SET plate_text = NULL, plate_confidence = NULL "
            "WHERE class NOT IN %s AND plate_text IS NOT NULL",
            (VEHICLE_CLASSES,),
        )
        log.info("Cleared %d plate reads from non-vehicle detections", cur.rowcount)
        conn.commit()

    where = ["d.thumbnail_key IS NOT NULL", "d.class IN %s"]
    params: list = [VEHICLE_CLASSES]
    if args.cams:
        where.append("c.external_id = ANY(%s)")
        params.append(args.cams.split(","))
    sql = (
        "SELECT d.id, c.external_id, d.thumbnail_key FROM detections d "
        "JOIN cameras c ON c.id = d.camera_id WHERE " + " AND ".join(where) +
        " ORDER BY d.id"
    )
    if args.limit:
        sql += f" LIMIT {args.limit}"
    cur.execute(sql, params)
    rows = cur.fetchall()
    log.info("%d vehicle crops to re-read", len(rows))
    if not rows:
        return

    pipe = DetectionPipeline()

    key_q: queue.Queue = queue.Queue()
    img_q: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
    stop = threading.Event()
    for r in rows:
        key_q.put(r)
    threads = [
        threading.Thread(target=fetch_worker, args=(key_q, img_q, stop), daemon=True)
        for _ in range(FETCH_THREADS)
    ]
    for t in threads:
        t.start()

    updates: list[tuple] = []
    found = legal = full = 0
    started = time.time()
    write_cur = conn.cursor()

    for i in range(len(rows)):
        try:
            det_id, cam, img = img_q.get(timeout=120)
        except queue.Empty:
            log.warning("Fetch stalled at %d/%d", i, len(rows))
            break
        if img is None:
            continue

        text, conf = pipe._extract_plate(img)
        if text:
            found += 1
            if matches_format(text):
                legal += 1
                if not is_probably_truncated(text):
                    full += 1
        # Written even when None: a read the fixed pipeline rejects should not be
        # left standing from the broken one.
        updates.append((text, conf, det_id))

        if len(updates) >= args.batch:
            if not args.dry_run:
                psycopg2.extras.execute_batch(
                    write_cur,
                    "UPDATE detections SET plate_text=%s, plate_confidence=%s WHERE id=%s",
                    updates, page_size=200,
                )
                conn.commit()
            updates.clear()
            rate = (i + 1) / max(time.time() - started, 1e-6)
            eta = (len(rows) - i - 1) / max(rate, 1e-6)
            log.info(
                "%d/%d  %.1f/s  eta %dm%02ds  reads=%d legal=%d full-tail=%d",
                i + 1, len(rows), rate, int(eta // 60), int(eta % 60), found, legal, full,
            )

    if updates and not args.dry_run:
        psycopg2.extras.execute_batch(
            write_cur,
            "UPDATE detections SET plate_text=%s, plate_confidence=%s WHERE id=%s",
            updates, page_size=200,
        )
        conn.commit()
    stop.set()

    elapsed = time.time() - started
    s = pipe.plate_report()
    print()
    print(f"{'':-<54}")
    print(f"  crops processed      {len(rows)}   in {elapsed/60:.1f} min")
    for k in ("seen", "no_box", "low_conf", "not_plate_shaped", "too_small",
              "too_short", "osd_reject", "error", "read", "valid"):
        if k in s:
            print(f"  {k:<20} {s[k]:>7}")
    print(f"{'':-<54}")
    print(f"  plate reads written  {found}")
    print(f"  legal Indian plates  {legal}")
    print(f"  with 4-digit tail    {full}")
    print()
    print("Next:  python -m ml.analytics.runner -v     # fold into consensus")


if __name__ == "__main__":
    main()
