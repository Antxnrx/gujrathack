"""Compare OCR models on real plate crops, with the plate detector fixed.

Established so far: on the chokepoint cameras the plates are plainly legible to
the eye — a WagonR at cam09 reads GJ03K7776 — and the pipeline returned
'0327K777'. So the footage is not the constraint there. This isolates which
stage is, by running the same crops through:

  A. the detector + OCR as the pipeline currently calls them
  B. the detector + OCR with the three call-level defects fixed
     (grayscale input, CPU execution provider, a 10-slot model)

Reads are scored by whether they are a legal Indian plate, which is a weak
proxy for correctness but an unbiased one — neither model was tuned for it.
"""
from __future__ import annotations

import argparse
import re

import cv2
import numpy as np
import psycopg2
from minio import Minio

from ml.plate_validator import matches_format, validate_plate

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"


def fetch_crops(cams: list[str], limit: int) -> list[tuple]:
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """SELECT c.external_id, d.thumbnail_key, d.plate_text,
                  (d.bbox->>'x2')::float - (d.bbox->>'x1')::float AS w
           FROM detections d JOIN cameras c ON c.id = d.camera_id
           WHERE c.external_id = ANY(%s)
             AND d.class IN ('car','truck','bus')
             AND (d.bbox->>'x2')::float - (d.bbox->>'x1')::float > 150
           ORDER BY (d.bbox->>'x2')::float - (d.bbox->>'x1')::float DESC
           LIMIT %s""",
        (cams, limit),
    )
    out = []
    for cam, key, old, w in cur.fetchall():
        raw = mc.get_object("setu-clips", key).read()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            out.append((cam, img, old, int(w)))
    return out


def sharpen(bgr: np.ndarray, scale: int = 3) -> np.ndarray:
    h, w = bgr.shape[:2]
    up = cv2.resize(bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    den = cv2.fastNlMeansDenoisingColored(up, None, 5, 5, 7, 21)
    blur = cv2.GaussianBlur(den, (0, 0), 3)
    return cv2.addWeighted(den, 1.5, blur, -0.5, 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="cam09,cam12,cam06,cam03")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    from fast_plate_ocr import LicensePlateRecognizer

    from ml.models import get_plate_detector

    det = get_plate_detector()
    models = {
        "mobile-vit (9 slots)": LicensePlateRecognizer("global-plates-mobile-vit-v2-model", device="cpu"),
        "cct-s-v2 (10 slots)": LicensePlateRecognizer("cct-s-v2-global-model", device="cpu"),
    }

    crops = fetch_crops(args.cams.split(","), args.limit)
    print(f"{len(crops)} vehicle crops wider than 150 px\n")

    stats = {name: {"read": 0, "valid": 0} for name in models}
    boxed = 0
    rows = []

    for cam, img, old, w in crops:
        res = det(img, verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            continue
        bi = int(res.boxes.conf.argmax())
        x1, y1, x2, y2 = (int(v) for v in res.boxes.xyxy[bi])
        pw, ph = x2 - x1, y2 - y1
        vh, vw = img.shape[:2]
        # A box covering most of the vehicle is not a plate. Indian plates are
        # roughly 2:1 to 5:1 and never more than half the vehicle's height.
        if pw <= 0 or ph <= 0 or ph > vh * 0.5 or pw > vw * 0.9:
            continue
        boxed += 1
        plate = sharpen(img[y1:y2, x1:x2])

        line = {"cam": cam, "old": old, "veh_w": w, "plate_px": f"{pw}x{ph}"}
        for name, m in models.items():
            cfg = m.config
            arr = (cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
                   if cfg.image_color_mode == "grayscale"
                   else cv2.cvtColor(plate, cv2.COLOR_BGR2RGB))
            try:
                p = m.run(arr, return_confidence=True)[0]
                txt = re.sub(r"[^A-Z0-9]", "", (p.plate or "").upper())
            except Exception:
                txt = ""
            if txt:
                stats[name]["read"] += 1
                if matches_format(txt) or validate_plate(txt):
                    stats[name]["valid"] += 1
            line[name] = txt or "—"
        rows.append(line)

    names = list(models)
    print(f"plate-shaped boxes found: {boxed} of {len(crops)}\n")
    print(f"{'cam':<7}{'veh':>6}{'plate':>10}  {'pipeline read':<15}{names[0]:<22}{names[1]:<22}")
    print("-" * 84)
    for r in rows[:30]:
        print(f"{r['cam']:<7}{r['veh_w']:>6}{r['plate_px']:>10}  "
              f"{(r['old'] or '—'):<15}{r[names[0]]:<22}{r[names[1]]:<22}")

    print("-" * 84)
    for name in names:
        s = stats[name]
        pct = 100 * s["valid"] / s["read"] if s["read"] else 0
        print(f"{name:<24} reads {s['read']:>3}   legal Indian plate {s['valid']:>3}  ({pct:.0f}%)")


if __name__ == "__main__":
    main()
