"""Is a plate physically readable on this estate, and where?

Model tuning cannot recover characters that were never sampled. An Indian plate
is ~500 mm wide on a ~4000 mm car, so the plate occupies about an eighth of the
vehicle's width in the image. OCR needs roughly 90-100 px of plate width to read
ten characters reliably, and degrades badly below ~60 px.

So the question that decides whether ANPR is achievable here is not "which OCR
model" but "how many pixels wide is the average vehicle". This walks daylight
segments on each camera and reports the distribution, then converts it into the
only number that matters: estimated plate width.
"""
from __future__ import annotations

import os
import statistics
import sys

import cv2

from ml.auth import get_cookie
from ml.models import VEHICLE_CLASSES, get_yolo
from ml.stream import _HLSFetcher

# Plate width as a fraction of vehicle width, and the OCR floor.
PLATE_FRACTION = 0.125
OCR_COMFORTABLE = 90
OCR_MARGINAL = 55

CAMS = sys.argv[1].split(",") if len(sys.argv) > 1 else [f"cam{i:02d}" for i in (1, 2, 4, 8, 12, 18, 22, 27)]
SEG = int(sys.argv[2]) if len(sys.argv) > 2 else 5700   # daylight

yolo = get_yolo()
f = _HLSFetcher(get_cookie())

print(f"{'camera':<9} {'vehicles':>8} {'median_w':>9} {'p90_w':>7} {'max_w':>7} "
      f"{'est_plate_p90':>14} {'verdict':>12}")
print("-" * 74)

for cam in CAMS:
    try:
        segs, key_url = f.fetch_manifest(cam)
        data = f.fetch_segment(segs[min(SEG, len(segs) - 1)], key_url)
    except Exception as exc:
        print(f"{cam:<9} unreachable ({type(exc).__name__})")
        continue

    path = f"_survey_{cam}.ts"
    with open(path, "wb") as fh:
        fh.write(data)

    widths: list[int] = []
    cap = cv2.VideoCapture(path)
    n = 0
    while n < 150:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        if n % 25 != 1:
            continue
        res = yolo(frame, verbose=False)[0]
        for box in res.boxes:
            cls = int(box.cls[0])
            if cls not in VEHICLE_CLASSES or VEHICLE_CLASSES[cls] == "person":
                continue
            if float(box.conf[0]) < 0.35:
                continue
            x1, _, x2, _ = (int(v) for v in box.xyxy[0])
            widths.append(x2 - x1)
    cap.release()
    os.remove(path)

    if not widths:
        print(f"{cam:<9} {'0':>8}  no vehicles in sample")
        continue

    widths.sort()
    med = statistics.median(widths)
    p90 = widths[int(len(widths) * 0.9) - 1]
    mx = widths[-1]
    est = p90 * PLATE_FRACTION
    verdict = ("readable" if est >= OCR_COMFORTABLE
               else "marginal" if est >= OCR_MARGINAL
               else "unreadable")
    print(f"{cam:<9} {len(widths):>8} {med:>9.0f} {p90:>7} {mx:>7} {est:>14.1f} {verdict:>12}")

print("-" * 74)
print(f"plate width estimated as {PLATE_FRACTION:.3f} x vehicle width; "
      f"OCR comfortable >= {OCR_COMFORTABLE}px, marginal >= {OCR_MARGINAL}px")
