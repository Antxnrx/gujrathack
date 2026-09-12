"""Fine-tune the plate detector on this estate's own geometry.

Starting point is the Koushim checkpoint rather than a COCO-pretrained YOLO. It
already knows what a licence plate is; what it does not know is *this* estate —
480p to 1080p feeds, plates 40 to 130 px wide, shot from a mast at a steep
downward angle, half of them at night under sodium light. Fine-tuning from a
plate-aware model needs far less data than teaching the concept from scratch,
which matters when the training set is a few thousand pseudo-labelled crops.

Augmentation is deliberately narrow. The usual defaults assume a photographer
who might hold the camera any way up; a fixed CCTV mast does not. Vertical flips
and large rotations would spend capacity on poses that never occur. What is left
is what genuinely varies here: exposure, colour temperature, small translations
and scale.

Afterwards, ``--evaluate`` re-runs the incumbent and the fine-tune over the same
held-out crops and reports recall for both, because a training curve says
nothing about whether the thing that was broken now works.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil

log = logging.getLogger("setu.train")

BASE_REPO = ("Koushim/yolov8-license-plate-detection", "best.pt")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="plate_dataset/data.yaml")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=320,
                    help="crops are small; 320 keeps plates several cells wide without waste")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument("--name", default="plate_setu")
    ap.add_argument("--device", default="")
    ap.add_argument("--install", action="store_true",
                    help="copy the best weights to models/plate_setu.pt when done")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    if not os.path.exists(args.data):
        raise SystemExit(
            f"No dataset at {args.data}. Run: python -m scripts.build_plate_dataset"
        )

    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO

    from ml.models import device

    dev = args.device or ("mps" if device.type == "mps" else str(device))
    log.info("Fine-tuning on %s from %s", dev, BASE_REPO[0])

    model = YOLO(hf_hub_download(*BASE_REPO))
    model.train(
        data=os.path.abspath(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        patience=args.patience,
        device=dev,
        name=args.name,
        exist_ok=True,
        # Fixed mast: the camera never rolls, never flips, and the plate is
        # always roughly horizontal. Spending augmentation on poses that cannot
        # occur costs capacity that this small a dataset does not have.
        flipud=0.0,
        fliplr=0.0,      # a mirrored plate is not a plate
        degrees=3.0,     # mast sway and mounting tolerance, not free rotation
        shear=2.0,
        perspective=0.0,
        # What does vary: time of day, sodium vs daylight, and how far down the
        # road the vehicle is when it enters frame.
        hsv_h=0.015, hsv_s=0.6, hsv_v=0.45,
        translate=0.12, scale=0.45,
        mosaic=0.6,      # helps small-object recall; plates are small objects
        close_mosaic=10,
        verbose=True,
    )

    best = os.path.join("runs", "detect", args.name, "weights", "best.pt")
    if os.path.exists(best):
        log.info("Best weights: %s", best)
        if args.install:
            os.makedirs("models", exist_ok=True)
            dest = os.path.join("models", "plate_setu.pt")
            shutil.copy(best, dest)
            log.info("Installed to %s", dest)
            print()
            print("To use it, set in .env:")
            print(f"    PLATE_MODEL={os.path.abspath(dest)}")
    else:
        log.warning("No best.pt produced at %s", best)


if __name__ == "__main__":
    main()
