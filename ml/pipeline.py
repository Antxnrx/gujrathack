import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import cv2
import numpy as np
import torch
from PIL import Image

from ml.config import settings
from ml.models import VEHICLE_CLASSES, device, get_clip, get_plate_detector, get_plate_ocr, get_yolo
from ml.plate_validator import validate_plate

log = logging.getLogger(__name__)

import threading

_inference_lock = threading.RLock()

# ── Plate geometry priors ────────────────────────────────────────────────────
# An Indian plate is 500x110 mm in its single-row form (4.5:1) and 280x200 mm
# in the two-row form used on motorcycles and some SUVs (1.4:1). The bounds are
# widened for perspective — a plate viewed off-axis compresses horizontally —
# but not so far that a whole vehicle passes as one.
_MIN_PLATE_ASPECT = 1.2
_MAX_PLATE_ASPECT = 7.0
# A plate is a small part of the vehicle carrying it. These reject the
# whole-vehicle boxes the current detector checkpoint returns.
_MAX_PLATE_HEIGHT_FRAC = 0.5
_MAX_PLATE_WIDTH_FRAC = 0.9
# Below this the characters were never sampled; see the per-camera survey in
# scripts/survey_plate_feasibility.py.
_MIN_PLATE_WIDTH = 40
# How far to widen the detector's plate box before cropping. Measured optimum;
# see _extract_plate for the sweep.
_PLATE_BOX_PAD = 0.25
# The shortest legal Indian plate is 6 characters. The previous floor of 4
# admitted fragments like "1427", which then outvoted real reads in consensus.
_MIN_READ_LEN = 6
# A read with no letters at all is the camera's burned-in clock, not a plate:
# every Indian registration begins with a two-letter state code.
_OSD_TEXT = re.compile(r"^[0-9]+$")

@dataclass
class Detection:
    id: str
    camera_id: str
    track_id: int | None
    class_name: str
    bbox: dict
    confidence: float
    plate_text: str | None
    plate_confidence: float | None
    embedding: list[float]
    thumbnail_key: str | None
    ts: datetime
    pts_ms: float           # position-in-segment in milliseconds (from CAP_PROP_POS_MSEC)
    dominant_color: str | None  # HSV colour bucket for NL search precision
    frame_w: int | None = None  # source frame width — lets normalised zone
    frame_h: int | None = None  # polygons be tested against pixel bboxes

class DetectionPipeline:
    # ── CLIP zero-shot label sets ─────────────────────────────────────────────
    # These text prompts are encoded once at init time. For each detection crop
    # the image embedding is compared (dot product) against each label embedding
    # and the argmax becomes the label. Cost: 8 dot products per detection.
    # Both color and class use this approach — no HSV math, no COCO-class limits.

    _CLASS_PROMPTS = [
        "auto rickshaw",       # India-specific — COCO doesn't have this
        "motorcycle or bike",
        "city bus or coach",
        "truck or lorry",
        "car or sedan or SUV",
        "tractor or farm vehicle",
        "pickup truck or van",
        "person walking",
    ]
    _CLASS_NAMES = [
        "auto_rickshaw", "motorcycle", "bus", "truck",
        "car", "tractor", "truck", "person",
    ]

    _COLOR_PROMPTS = [
        "a red coloured vehicle",
        "a blue coloured vehicle",
        "a white coloured vehicle",
        "a yellow coloured vehicle",
        "a black coloured vehicle",
        "a silver or grey coloured vehicle",
        "a green coloured vehicle",
        "an orange coloured vehicle",
    ]
    _COLOR_NAMES = [
        "red", "blue", "white", "yellow", "black", "silver", "green", "orange",
    ]

    def __init__(self):
        self.yolo = get_yolo()
        self.clip, self.clip_preprocess = get_clip()
        self.plate_detector = get_plate_detector()
        self.plate_ocr = get_plate_ocr()

        # Where plate reads are lost. Each key is a different remedy: no_box and
        # not_plate_shaped mean the detector; too_small means camera siting;
        # too_short and error mean the OCR itself.
        self._plate_stats = {
            "seen": 0, "no_box": 0, "low_conf": 0, "bad_box": 0,
            "not_plate_shaped": 0, "too_small": 0, "no_read": 0,
            "too_short": 0, "osd_reject": 0, "read": 0, "valid": 0, "error": 0,
        }

        # Pre-encode CLIP label sets once at startup.
        # Shape: (N_labels, 512), L2-normalized — ready for dot-product scoring.
        import open_clip
        # From settings, not a literal: the checkpoint moved to ViT-B-32-quickgelu
        # and a tokenizer pinned to a different architecture name is exactly the
        # kind of drift backend/app/clip_text.py exists to prevent.
        tokenizer = open_clip.get_tokenizer(settings.clip_model)
        with torch.no_grad():
            cls_tokens = tokenizer(self._CLASS_PROMPTS).to(device)
            cls_embs = self.clip.encode_text(cls_tokens)
            cls_embs /= cls_embs.norm(dim=-1, keepdim=True)
            self._class_label_embs = cls_embs  # (8, 512) on device

            col_tokens = tokenizer(self._COLOR_PROMPTS).to(device)
            col_embs = self.clip.encode_text(col_tokens)
            col_embs /= col_embs.norm(dim=-1, keepdim=True)
            self._color_label_embs = col_embs  # (8, 512) on device

        # One isolated ByteTracker per DetectionPipeline instance.
        # worker.py creates one pipeline per camera thread, so this is effectively
        # one tracker per camera — no shared state, no cross-camera track_id bleed.
        from ultralytics.trackers import BYTETracker
        from ultralytics.utils import IterableSimpleNamespace
        _tracker_args = IterableSimpleNamespace(
            track_high_thresh=0.5,    # min conf to enter high-conf association
            track_low_thresh=0.1,     # min conf for low-conf second-pass association
            new_track_thresh=0.6,     # min conf to create a new track
            track_buffer=30,          # frames to keep a lost track (30 ≈ 5s at 6 FPS)
            match_thresh=0.8,         # IoU threshold for first-pass association
            fuse_score=True,          # fuse detection score into track score
            frame_rate=6,             # approximate processed FPS (frame_skip=5 at 30 FPS)
        )
        self.tracker = BYTETracker(_tracker_args)

    def process_frame(self, frame_bgr: np.ndarray, camera_id: str, pts_ms: float) -> list[Detection]:
        with _inference_lock:
            results = self.yolo.predict(
                frame_bgr, 
                classes=list(VEHICLE_CLASSES.keys()), 
                conf=settings.detection_conf_threshold, 
                verbose=False, 
            )
        
        detections = []
        if not results or not results[0].boxes:
            return detections

        yolo_result = results[0]
        boxes = yolo_result.boxes
        ts = datetime.now(UTC)

        # ── ByteTrack: feed YOLO boxes into this camera's isolated tracker ────
        # tracker.update() returns an (N, 7+) array: [x1,y1,x2,y2,track_id,conf,cls_id]
        # Each row's original index into `boxes` is stored in row[-1] (the `idx` field).
        # We build a dict {box_idx: track_id} for fast lookup below.
        track_id_by_idx: dict[int, int] = {}
        try:
            tracked = self.tracker.update(boxes.cpu(), frame_bgr)
            if tracked is not None and len(tracked) > 0:
                # tracked array columns: x1,y1,x2,y2, track_id, conf, cls, *extra
                # The tracker preserves the detection index in an attribute on STrack;
                # the returned numpy array has it at column index 4 as the track ID.
                # Ultralytics BYTETracker returns shape (N,7): xyxy + tid + conf + cls
                for row in tracked:
                    if len(row) >= 7:
                        # Find matching box by IoU with x1y1x2y2
                        tx1, ty1, tx2, ty2 = float(row[0]), float(row[1]), float(row[2]), float(row[3])
                        tid = int(row[4])
                        # Match back to original box index by closest xyxy
                        best_idx, best_iou = -1, -1.0
                        for i, box in enumerate(boxes):
                            bx1, by1, bx2, by2 = box.xyxy[0].tolist()
                            inter_x1, inter_y1 = max(tx1, bx1), max(ty1, by1)
                            inter_x2, inter_y2 = min(tx2, bx2), min(ty2, by2)
                            inter = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)
                            union = ((tx2-tx1)*(ty2-ty1) + (bx2-bx1)*(by2-by1) - inter)
                            iou = inter / union if union > 0 else 0.0
                            if iou > best_iou:
                                best_iou, best_idx = iou, i
                        if best_idx >= 0 and best_iou > 0.3:
                            track_id_by_idx[best_idx] = tid
        except Exception as e:
            log.warning("ByteTrack update failed: %s — falling back to no tracking", e)
        # ─────────────────────────────────────────────────────────────────────

        h, w = frame_bgr.shape[:2]
        for i, box in enumerate(boxes):
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0])
            cls_id = int(box.cls[0])
            track_id = track_id_by_idx.get(i)  # None if tracker didn't assign this box
            
            # bounds check
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            
            crop = frame_bgr[y1:y2, x1:x2]
            if crop.size == 0 or crop.shape[0] < 10 or crop.shape[1] < 10:
                continue

            embedding_tensor, embedding = self._embed_crop(crop)
            if not embedding:
                continue

            # ── CLIP zero-shot color + class ──────────────────────────────────
            # embedding_tensor is shape (1, 512), already L2-normalised, on device.
            # Label embs are (N, 512). Dot product = cosine similarity.
            with torch.no_grad():
                color_scores = (embedding_tensor @ self._color_label_embs.T)[0]
                color_idx = int(color_scores.argmax().item())
                dominant_color = self._COLOR_NAMES[color_idx]

                class_scores = (embedding_tensor @ self._class_label_embs.T)[0]
                class_idx = int(class_scores.argmax().item())
                clip_class_name = self._CLASS_NAMES[class_idx]
            # ──────────────────────────────────────────────────────────────────

            plate_text, plate_conf = None, None
            # Run plate OCR on all detections; CLIP class may catch vehicles YOLO
            # labelled as 'person' or vice-versa, so don't gate on cls_id alone.
            plate_text, plate_conf = self._extract_plate(crop)

            detections.append(Detection(
                id=str(uuid.uuid4()),
                camera_id=camera_id,
                track_id=track_id,
                class_name=clip_class_name,   # CLIP zero-shot, not YOLO COCO label
                bbox={'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2},
                confidence=conf,
                plate_text=plate_text,
                plate_confidence=plate_conf,
                embedding=embedding,
                thumbnail_key=None,
                ts=ts,
                pts_ms=pts_ms,
                dominant_color=dominant_color,  # CLIP zero-shot, not HSV buckets
                frame_w=w,
                frame_h=h,
            ))

        return detections

    def _embed_crop(self, crop_bgr: np.ndarray) -> tuple[torch.Tensor | None, list[float]]:
        """Encode a BGR crop with CLIP. Returns (tensor, list) — tensor stays on
        device for zero-shot label scoring; list is stored in DB."""
        try:
            crop_rgb = crop_bgr[:, :, ::-1]
            pil_img = Image.fromarray(crop_rgb)
            img_tensor = self.clip_preprocess(pil_img).unsqueeze(0).to(device)
            with torch.no_grad():
                embed = self.clip.encode_image(img_tensor)
                embed /= embed.norm(dim=-1, keepdim=True)
            return embed, embed[0].cpu().tolist()
        except Exception:
            return None, []

    def _extract_color(self, crop_bgr: np.ndarray) -> str | None:
        """Dominant colour of the crop using HSV histogram bucketing.

        Returns a colour name string suitable for NL search filtering:
        red | white | black | silver | blue | yellow | green | orange | unknown
        """
        try:
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            h = hsv[:, :, 0].astype(np.float32)  # 0–179 in OpenCV
            s = hsv[:, :, 1].astype(np.float32)  # 0–255
            v = hsv[:, :, 2].astype(np.float32)  # 0–255

            total = h.size
            if total == 0:
                return "unknown"

            # ── Achromatic buckets (low saturation) ───────────────────────
            low_s_mask = s < 50   # unsaturated pixels
            low_s_frac = low_s_mask.sum() / total

            if low_s_frac > 0.55:
                # Mostly achromatic — classify by brightness
                mean_v = float(v[low_s_mask].mean()) if low_s_mask.any() else float(v.mean())
                if mean_v < 60:
                    return "black"
                elif mean_v < 140:
                    return "silver"
                else:
                    return "white"

            # ── Chromatic: use hue histogram on saturated pixels only ─────
            sat_mask = s >= 50
            if not sat_mask.any():
                return "unknown"

            h_sat = h[sat_mask]

            # OpenCV hue: 0–179 (degrees / 2). Bucket boundaries in that space:
            #   red:    [0,10) ∪ [170,180)
            #   orange: [10,20)
            #   yellow: [20,35)
            #   green:  [35,85)
            #   blue:   [85,130)
            #   (purple/violet skipped — rare in vehicles)
            buckets = {
                "red":    ((h_sat < 10) | (h_sat >= 170)).sum(),
                "orange": ((h_sat >= 10) & (h_sat < 20)).sum(),
                "yellow": ((h_sat >= 20) & (h_sat < 35)).sum(),
                "green":  ((h_sat >= 35) & (h_sat < 85)).sum(),
                "blue":   ((h_sat >= 85) & (h_sat < 130)).sum(),
            }
            dominant = max(buckets, key=lambda k: buckets[k])
            return dominant
        except Exception:
            return "unknown"

    def _extract_plate(self, vehicle_crop: np.ndarray) -> tuple[str | None, float | None]:
        """Read a plate from one vehicle crop, or return None with a reason logged.

        Structured as four gates, because each rejects a different failure and
        collapsing them loses the ability to say *why* ANPR produced nothing:

          1. the detector proposed nothing
          2. it proposed something that is not plate-shaped
          3. the plate is too few pixels across to carry ten characters
          4. the OCR read something too short to be a plate

        Only the fourth is a genuine OCR failure, and only measuring them
        separately tells you whether to change the detector, the camera siting,
        or the OCR.
        """
        stats = self._plate_stats
        stats["seen"] += 1
        try:
            res = self.plate_detector(vehicle_crop, verbose=False, stream=False)
            if not res or res[0].boxes is None or len(res[0].boxes) == 0:
                stats["no_box"] += 1
                return None, None

            boxes = res[0].boxes
            best_idx = int(boxes.conf.argmax())
            plate_conf = float(boxes.conf[best_idx])
            if plate_conf < settings.plate_conf_threshold:
                stats["low_conf"] += 1
                return None, None

            px1, py1, px2, py2 = map(int, boxes.xyxy[best_idx].tolist())
            pw, ph = px2 - px1, py2 - py1
            vh, vw = vehicle_crop.shape[:2]

            # ── Gate 2: is this box even plate-shaped? ───────────────────────
            # The FarAwayFer checkpoint frequently returns a box covering the
            # whole vehicle — on real footage it produced entire vans and a bus's
            # row of marker lights. Feeding that to the OCR asks it to find a
            # plate inside a car, which it cannot, and the resulting six-character
            # noise then poisons the track's consensus vote. An Indian plate is
            # 500x110 mm single-row (~4.5:1) or 280x200 mm two-row (~1.4:1), and
            # is never half the height of the vehicle carrying it.
            if pw <= 0 or ph <= 0:
                stats["bad_box"] += 1
                return None, None
            aspect = pw / ph
            if not (_MIN_PLATE_ASPECT <= aspect <= _MAX_PLATE_ASPECT) \
                    or ph > vh * _MAX_PLATE_HEIGHT_FRAC \
                    or pw > vw * _MAX_PLATE_WIDTH_FRAC:
                stats["not_plate_shaped"] += 1
                return None, None

            # ── Gate 3: enough pixels to be worth reading? ───────────────────
            # Measured on this estate: reads are reliable above roughly 100 px of
            # plate width and are noise below ~55. Attempting them anyway is not
            # free — a confident wrong read is worse than an honest null, because
            # consensus will happily average several of them into a plausible
            # plate that was never on any vehicle.
            if pw < _MIN_PLATE_WIDTH:
                stats["too_small"] += 1
                return None, None

            # Widen the box before cropping. The detector localises the plate
            # tightly and its edges sit *inside* the characters often enough to
            # cost the last of them — which on an Indian plate is the registration
            # number, the part that identifies the vehicle.
            #
            # Measured on 70 real crops from the chokepoint cameras, sweeping the
            # pad and counting reads that come back as a full ten characters:
            #
            #     pad     0%   15%   25%   35%   45%
            #     10-char  0     4    18    11     6
            #     4-digit  1     4    12     3     1
            #
            # Zero full-length reads at a tight crop, eighteen at 25%. Past that
            # the surrounding bodywork starts to compete with the plate and it
            # falls away again.
            pad_x, pad_y = int(pw * _PLATE_BOX_PAD), int(ph * _PLATE_BOX_PAD)
            ch_, cw_ = vehicle_crop.shape[:2]
            plate_crop = vehicle_crop[
                max(0, py1 - pad_y):min(ch_, py2 + pad_y),
                max(0, px1 - pad_x):min(cw_, px2 + pad_x),
            ]
            if plate_crop.size == 0:
                stats["bad_box"] += 1
                return None, None

            # The backend owns its own colour mode, resize and normalisation —
            # see ml/ocr.py. The pipeline hands over a BGR plate crop and gets
            # back a string, which is the only contract that survived changing
            # the model underneath it.
            read = self.plate_ocr.read(plate_crop)
            if read is None or not read.text:
                stats["no_read"] += 1
                return None, None
            clean_text = read.text

            # ── Gate 4: long enough to be a plate at all? ────────────────────
            # The old floor was 4, which admitted fragments like "1427" and
            # "5412". The shortest legal Indian plate is 6 characters
            # (e.g. DL1C1234 is 8; a 1-digit series on a 1-digit RTO is 6).
            if len(clean_text) < _MIN_READ_LEN:
                stats["too_short"] += 1
                return None, None

            # ── Gate 5: is this the camera's own clock? ──────────────────────
            # These cameras burn a date and time into the frame, and on a large
            # vehicle crop the overlay lands inside the box. It is high-contrast
            # text on a dark rectangle, which is exactly what a plate detector is
            # trained to find — observed reads include "18062026" and
            # "1406622226", i.e. 18/06/2026 and 14/06/2026.
            #
            # Every Indian plate begins with two letters, so an all-digit read is
            # never one. Rejecting it here matters more than it looks: such a read
            # is not merely useless, it joins the track's consensus vote and drags
            # the result toward a number that was never on a vehicle.
            if _OSD_TEXT.match(clean_text):
                stats["osd_reject"] += 1
                return None, None

            ocr_conf = read.confidence

            stats["read"] += 1
            validated = validate_plate(clean_text)
            if validated:
                stats["valid"] += 1
                return validated, min(plate_conf, ocr_conf)

            # An unvalidated read is still returned. It goes to the track-level
            # consensus in ml/analytics/anpr.py, which aligns several imperfect
            # reads against each other and is the stage designed to recover the
            # plate — discarding them here would starve exactly the mechanism
            # that fixes them.
            return clean_text, min(plate_conf, ocr_conf)

        except Exception as exc:
            # Loud, and once. A broken OCR previously logged at warning level per
            # frame and returned None, which is indistinguishable from "no plate
            # was visible" — so an entirely non-functional ANPR looked exactly
            # like difficult footage. It is not a per-frame condition; it is a
            # configuration fault and it should read as one.
            stats["error"] += 1
            if stats["error"] == 1 or stats["error"] % 500 == 0:
                log.error(
                    "Plate OCR is failing (%d times so far): %s: %s — ANPR is "
                    "producing nothing. This is a configuration fault, not footage.",
                    stats["error"], type(exc).__name__, exc,
                )
            return None, None

    def plate_report(self) -> dict:
        """Where plate reads were lost, as counts. Logged at the end of a run."""
        return dict(self._plate_stats)
