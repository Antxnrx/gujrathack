import logging
import os
import threading

import torch

from ml.config import settings

log = logging.getLogger(__name__)

VEHICLE_CLASSES = {0: 'person', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck'}


def _pick_device() -> torch.device:
    """Best available accelerator, in order of throughput.

    CUDA first — it is the deployment target and the only one of the three that
    runs 30 camera threads at once. Metal (MPS) second: it is what an Apple
    Silicon development machine actually has, and leaving it out meant every
    model silently fell back to CPU here, which is roughly an order of magnitude
    slower and made local indexing look infeasible when it is not.

    MPS is opt-out via SETU_DEVICE=cpu, because a handful of torch ops still
    have no Metal kernel and fall back mid-graph; if that ever bites a model,
    the escape hatch should not require an edit.
    """
    forced = os.getenv("SETU_DEVICE", "").strip().lower()
    if forced:
        log.info("Device forced by SETU_DEVICE=%s", forced)
        return torch.device(forced)
    if torch.cuda.is_available():
        return torch.device('cuda')
    if torch.backends.mps.is_available():
        return torch.device('mps')
    return torch.device('cpu')


device = _pick_device()
log.info("Inference device: %s", device)

_yolo = None
_yolo_lock = threading.Lock()

_clip = None
_clip_preprocess = None
_clip_lock = threading.Lock()

_plate_detector = None
_plate_lock = threading.Lock()

_plate_ocr = None
_ocr_lock = threading.Lock()

def get_yolo():
    global _yolo
    with _yolo_lock:
        if _yolo is None:
            from ultralytics import YOLO
            log.info(f"Loading YOLO model {settings.yolo_model}")
            _yolo = YOLO(settings.yolo_model).to(device)
    return _yolo

def get_clip():
    global _clip, _clip_preprocess
    with _clip_lock:
        if _clip is None:
            import open_clip
            log.info(f"Loading CLIP model {settings.clip_model}")
            _clip, _, _clip_preprocess = open_clip.create_model_and_transforms(
                settings.clip_model, pretrained=settings.clip_pretrained
            )
            _clip = _clip.to(device).eval()
    return _clip, _clip_preprocess

def get_plate_detector():
    global _plate_detector
    with _plate_lock:
        if _plate_detector is None:
            import os

            from ultralytics import YOLO
            
            model_path = settings.plate_model
            if "/" in model_path and not os.path.exists(model_path):
                from huggingface_hub import hf_hub_download
                log.info(f"Downloading plate detector from HF Hub: {model_path}")
                # We expect format 'repo_id/filename' or just 'repo_id' with 'best.pt'
                parts = model_path.split("/")
                repo_id = "/".join(parts[:2])
                filename = parts[2] if len(parts) > 2 else "best_yolov8n.pt" if repo_id == "FarAwayFer/yolov8n_license_plate_recognition" else "best.pt"
                model_path = hf_hub_download(repo_id, filename)
                
            log.info(f"Loading plate detector {model_path}")
            _plate_detector = YOLO(model_path).to(device)
    return _plate_detector

def get_plate_ocr():
    """The plate OCR, pinned to the CPU execution provider.

    Not an oversight — onnxruntime's CoreML provider mis-partitions this graph on
    Apple Silicon and fails at inference with

        Size (9) of dimension (2) is not in allowed range (8..8)

    which surfaces as an ONNXRuntimeError on *every* call, so ANPR returns
    nothing at all on a Mac while looking like a footage problem. The OCR model
    is a few megabytes and runs comfortably on CPU; the GPU belongs to YOLO and
    CLIP, which are the stages that actually need it.

    ``SETU_OCR_DEVICE`` overrides for a CUDA box, where the provider is fine.
    """
    global _plate_ocr
    with _ocr_lock:
        if _plate_ocr is None:
            from fast_plate_ocr import LicensePlateRecognizer
            ocr_device = os.getenv("SETU_OCR_DEVICE", "cpu")
            log.info("Loading plate OCR %s on %s", settings.plate_ocr_model, ocr_device)
            _plate_ocr = LicensePlateRecognizer(settings.plate_ocr_model, device=ocr_device)
            cfg = _plate_ocr.config
            log.info(
                "Plate OCR config: %d slots, %dx%d, %s",
                cfg.max_plate_slots, cfg.img_width, cfg.img_height, cfg.image_color_mode,
            )
            if cfg.max_plate_slots < 10:
                log.warning(
                    "Plate OCR has only %d character slots. A modern Indian plate is 10 "
                    "characters (GJ01AB1234), so this model cannot represent one and will "
                    "silently truncate every full-length read.",
                    cfg.max_plate_slots,
                )
    return _plate_ocr

def warm_all_models():
    get_yolo()
    get_clip()
    get_plate_detector()
    get_plate_ocr()
