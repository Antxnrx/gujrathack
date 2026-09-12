"""Compare plate-detector checkpoints on this estate's real crops.

The OCR is fixed and the remaining loss is localisation: on 120 real vehicle
crops the incumbent checkpoint returned no box at all for 69 of them, and for
several of the rest returned the whole vehicle. Upscaling does not recover it
(39 -> 40 boxes at 3x), so the checkpoint is the constraint, not the input.

This measures each candidate end-to-end, because detector recall on its own is
the wrong target — a detector that boxes everything scores perfect recall and
produces nothing readable. The chain that matters is:

    box found -> survives the geometry gate -> OCR returns >= 6 chars
              -> string is a legal Indian plate -> tail is 4 digits

Crops come from MinIO, so no camera and no GPU box is needed.
"""
from __future__ import annotations

import argparse

import cv2
import numpy as np
import psycopg2
from minio import Minio

from ml.plate_validator import format_typicality, is_probably_truncated, matches_format

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"

# repo id -> filename within the repo.
CANDIDATES = {
    "FarAwayFer (incumbent)": ("FarAwayFer/yolov8n_license_plate_recognition", "best_yolov8n.pt"),
    "morsetechlab v11n": ("morsetechlab/yolov11-license-plate-detection", "license-plate-finetune-v1n.pt"),
    "morsetechlab v11m": ("morsetechlab/yolov11-license-plate-detection", "license-plate-finetune-v1m.pt"),
    "Koushim v8 (MIT)": ("Koushim/yolov8-license-plate-detection", "best.pt"),
}

# Same gate the pipeline applies, kept in sync deliberately: a detector is only
# useful here if it clears the checks production will apply to it.
MIN_ASPECT, MAX_ASPECT = 1.2, 7.0
MAX_H_FRAC, MAX_W_FRAC = 0.5, 0.9
MIN_PLATE_W = 40
TARGET_W, MAX_UPSCALE = 180, 4.0


def load_crops(cams: list[str], limit: int, min_w: int) -> list[tuple[str, np.ndarray, str | None, int]]:
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    cur = psycopg2.connect(DB).cursor()
    cur.execute(
        """SELECT c.external_id, d.thumbnail_key, d.plate_text,
                  (d.bbox->>'x2')::float - (d.bbox->>'x1')::float AS w
           FROM detections d JOIN cameras c ON c.id = d.camera_id
           WHERE c.external_id = ANY(%s)
             AND d.class IN ('car','truck','bus','auto_rickshaw')
             AND (d.bbox->>'x2')::float - (d.bbox->>'x1')::float >= %s
           ORDER BY (d.bbox->>'x2')::float - (d.bbox->>'x1')::float DESC
           LIMIT %s""",
        (cams, min_w, limit),
    )
    out = []
    for cam, key, old, w in cur.fetchall():
        img = cv2.imdecode(
            np.frombuffer(mc.get_object("setu-clips", key).read(), np.uint8), cv2.IMREAD_COLOR
        )
        if img is not None:
            out.append((cam, img, old, int(w)))
    return out


def prepare(plate: np.ndarray, colour: str) -> np.ndarray:
    h, w = plate.shape[:2]
    scale = max(1.0, min(MAX_UPSCALE, TARGET_W / max(w, 1)))
    if scale > 1.0:
        plate = cv2.resize(plate, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    if colour == "grayscale":
        g = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
        den = cv2.bilateralFilter(g, 9, 75, 75)
        return cv2.addWeighted(den, 1.5, cv2.GaussianBlur(den, (0, 0), 3), -0.5, 0)
    lab = cv2.cvtColor(plate, cv2.COLOR_BGR2LAB)
    lum, a, b = cv2.split(lab)
    lum = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lum)
    merged = cv2.cvtColor(cv2.merge((lum, a, b)), cv2.COLOR_LAB2BGR)
    sharp = cv2.addWeighted(merged, 1.5, cv2.GaussianBlur(merged, (0, 0), 3), -0.5, 0)
    return cv2.cvtColor(sharp, cv2.COLOR_BGR2RGB)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="cam09,cam12,cam06,cam03,cam04")
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--min-width", type=int, default=100)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--show", type=int, default=0, help="print this many reads per model")
    args = ap.parse_args()

    from fast_plate_ocr import LicensePlateRecognizer
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO

    from ml.models import device

    ocr = LicensePlateRecognizer("cct-s-v2-global-model", device="cpu")
    colour = ocr.config.image_color_mode

    crops = load_crops(args.cams.split(","), args.limit, args.min_width)
    print(f"{len(crops)} crops, vehicles >= {args.min_width}px, cams {args.cams}")
    print(f"OCR: cct-s-v2 ({ocr.config.max_plate_slots} slots, {colour}), detector conf >= {args.conf}\n")

    print(f"{'detector':<24}{'boxed':>7}{'plate-shaped':>14}{'read':>6}{'legal':>7}{'full tail':>11}")
    print("-" * 69)

    samples: dict[str, list] = {}
    for name, (repo, fname) in CANDIDATES.items():
        try:
            path = hf_hub_download(repo, fname)
            det = YOLO(path).to(device)
        except Exception as exc:
            print(f"{name:<24} unavailable ({type(exc).__name__})")
            continue

        boxed = shaped = read = legal = full = 0
        rows = []
        for cam, img, old, vw in crops:
            r = det(img, verbose=False, conf=args.conf)[0]
            if r.boxes is None or len(r.boxes) == 0:
                continue
            boxed += 1
            bi = int(r.boxes.conf.argmax())
            x1, y1, x2, y2 = (int(v) for v in r.boxes.xyxy[bi])
            pw, ph = x2 - x1, y2 - y1
            vh, vwid = img.shape[:2]
            if pw <= 0 or ph <= 0:
                continue
            if not (MIN_ASPECT <= pw / ph <= MAX_ASPECT) or ph > vh * MAX_H_FRAC \
                    or pw > vwid * MAX_W_FRAC or pw < MIN_PLATE_W:
                continue
            shaped += 1
            crop = img[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                continue
            try:
                pred = ocr.run(prepare(crop, colour), return_confidence=True)[0]
                txt = "".join(ch for ch in (pred.plate or "").upper() if ch.isalnum())
            except Exception:
                continue
            if len(txt) < 6:
                continue
            read += 1
            if matches_format(txt):
                legal += 1
                if not is_probably_truncated(txt):
                    full += 1
            rows.append((cam, vw, f"{pw}x{ph}", old, txt,
                         matches_format(txt), format_typicality(txt)))

        samples[name] = rows
        print(f"{name:<24}{boxed:>7}{shaped:>14}{read:>6}{legal:>7}{full:>11}")

    print("-" * 69)
    print(f"of {len(crops)} crops. 'full tail' = legal AND four trailing digits.")

    if args.show:
        for name, rows in samples.items():
            keep = [r for r in rows if r[5]] or rows
            if not keep:
                continue
            print(f"\n{name}")
            print(f"  {'cam':<7}{'veh':>5}{'plate':>9}  {'was':<12}{'now':<12}{'typ':>6}")
            for cam, vw, px, old, txt, ok, typ in keep[:args.show]:
                print(f"  {cam:<7}{vw:>5}{px:>9}  {(old or '—'):<12}{txt:<12}{typ:>6.2f}")


if __name__ == "__main__":
    main()
