"""What does the OCR return on plates a human can read outright?

Inspecting cam06's crops settles the argument the metrics could not. Its plates
are small — 43 to 97 px wide — but sharp, face-on and unambiguous:
GJ11BR2912, GJ27EC9620, GJ32AG0028. Ten characters, four-digit tail, exactly the
canonical form.

If the OCR cannot read those, no detector change and no camera change helps, and
the remaining work is the OCR stage. This runs several models and several
preprocessing variants over the same crops so the answer is attributable rather
than inferred, and writes the crops out so a human can check the model against
what is actually on the plate.
"""
from __future__ import annotations

import argparse
import os

import cv2
import numpy as np
import psycopg2
from minio import Minio
from huggingface_hub import hf_hub_download
from ultralytics import YOLO

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"


def variants(bgr: np.ndarray, colour: str, target_w: int) -> dict[str, np.ndarray]:
    """Preprocessing variants, from none to the pipeline's full treatment."""
    h, w = bgr.shape[:2]
    scale = max(1.0, target_w / max(w, 1))
    up = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

    out: dict[str, np.ndarray] = {}

    def finish(img: np.ndarray) -> np.ndarray:
        if colour == "grayscale":
            return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)

    out["raw"] = finish(bgr)
    out["upscaled"] = finish(up)

    g = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    out["clahe"] = finish(clahe)

    den = cv2.bilateralFilter(clahe, 9, 75, 75)
    sharp = cv2.addWeighted(den, 1.5, cv2.GaussianBlur(den, (0, 0), 3), -0.5, 0)
    out["clahe+sharp"] = finish(sharp)

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="cam06")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--target-w", type=int, default=180)
    ap.add_argument("--outdir", default="artifacts/clear_plates")
    args = ap.parse_args()

    from fast_plate_ocr import LicensePlateRecognizer
    from ml.models import device

    models = {}
    for name, repo in [("cct-s-v2", "cct-s-v2-global-model"),
                       ("cct-xs-v2", "cct-xs-v2-global-model"),
                       ("mobile-vit", "global-plates-mobile-vit-v2-model")]:
        try:
            models[name] = LicensePlateRecognizer(repo, device="cpu")
        except Exception as exc:
            print(f"  {name} unavailable: {type(exc).__name__}")

    det = YOLO(hf_hub_download("Koushim/yolov8-license-plate-detection", "best.pt")).to(device)
    mc = Minio("localhost:9000", access_key="setu", secret_key="setu_dev_only", secure=False)
    cur = psycopg2.connect(DB).cursor()
    cur.execute(
        """SELECT c.external_id, d.thumbnail_key,
                  (d.bbox->>'x2')::float - (d.bbox->>'x1')::float AS w
           FROM detections d JOIN cameras c ON c.id = d.camera_id
           WHERE c.external_id = ANY(%s) AND d.class IN ('car','truck','bus')
             AND (d.bbox->>'x2')::float - (d.bbox->>'x1')::float BETWEEN 120 AND 300
           ORDER BY random() LIMIT 200""",
        (args.cams.split(","),),
    )

    os.makedirs(args.outdir, exist_ok=True)
    found = 0
    print(f"{'#':<4}{'plate px':>10}  " + "".join(f"{m:<22}" for m in models))
    print("-" * (14 + 22 * len(models)))

    for cam, key, w in cur.fetchall():
        if found >= args.samples:
            break
        img = cv2.imdecode(
            np.frombuffer(mc.get_object("setu-clips", key).read(), np.uint8), cv2.IMREAD_COLOR
        )
        if img is None:
            continue
        r = det(img, verbose=False, conf=0.25)[0]
        if r.boxes is None or len(r.boxes) == 0:
            continue
        bi = int(r.boxes.conf.argmax())
        x1, y1, x2, y2 = (int(v) for v in r.boxes.xyxy[bi])
        pw, ph = x2 - x1, y2 - y1
        if ph <= 0 or pw < 40 or not (1.2 <= pw / ph <= 7.0):
            continue
        crop = img[max(0, y1):y2, max(0, x1):x2]
        if crop.size == 0:
            continue

        found += 1
        # Written out so the reads below can be checked against the actual plate.
        cv2.imwrite(
            os.path.join(args.outdir, f"{found:02d}_{cam}_{pw}x{ph}.png"),
            cv2.resize(crop, (420, int(420 * ph / pw)), interpolation=cv2.INTER_CUBIC),
        )

        cells = []
        for name, m in models.items():
            best = ""
            for vname, arr in variants(crop, m.config.image_color_mode, args.target_w).items():
                try:
                    p = m.run(arr, return_confidence=True)[0]
                    txt = "".join(ch for ch in (p.plate or "").upper() if ch.isalnum())
                except Exception:
                    continue
                if len(txt) > len(best):
                    best = f"{txt}({vname[:5]})"
            cells.append(f"{best or '—':<22}")
        print(f"{found:<4}{f'{pw}x{ph}':>10}  " + "".join(cells))

    print(f"\ncrops written to {args.outdir}/ — compare each read against the image")


if __name__ == "__main__":
    main()
