"""Plate OCR backends behind one interface.

Why an interface at all
-----------------------
``_extract_plate`` used to reach into the OCR's config to decide whether to hand
it grayscale or RGB, and to guess how much to upscale. That is the pipeline doing
a model's job, and it broke the moment the model changed: a checkpoint wanting
RGB got three channels where it expected one, raised ``InvalidArgument`` on every
call, and ANPR returned nothing while looking like a footage problem.

Each backend below owns its own preprocessing. The pipeline hands over a BGR crop
of the plate and gets back a string and a confidence. Nothing else crosses the
boundary.

The two backends
----------------
``fast-plate-ocr`` (``cct-s-v2-global-model``) is a *global* model — trained on
plates from everywhere, which means it has seen very few Gujarat plates and none
of the two-row commercial format.

``Awiros/anpr-ocr`` is India-specific: PP-OCRv5 backbone, 37M parameters,
fine-tuned on 558,767 Indian plates across all state codes, single and dual row,
Apache-2.0. Measured head to head on this estate's own crops, against plates read
by a human:

    exact match        global 3/9 (33%)    India-specific 7/9 (78%)
    character error    global 38.9%        India-specific 13.3%

For context, the ICPR 2026 competition on low-resolution plate recognition
reports prior state of the art at 50-60% and its winner at 82.13%, so 78% on real
CCTV is a reasonable place to be rather than a suspicious one.

The global model is kept as the default fallback because the India-specific one
carries a PaddlePaddle dependency and needs the PaddleOCR sources for its
architecture. A deployment that cannot take that keeps working, less accurately,
and ``/api/health`` says which is loaded.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Plates narrower than this get a mild upscale so a model's own resize has
# something to work with. Wider ones are passed through: interpolating detail
# that is already there only adds noise.
_SMALL_PLATE_WIDTH = 120
_MAX_UPSCALE = 2.5


@dataclass(frozen=True)
class Read:
    """One OCR result. ``text`` is uppercase alphanumerics only."""

    text: str
    confidence: float
    backend: str


def _clean(raw: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def _upscale_small(bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    if w <= 0 or w >= _SMALL_PLATE_WIDTH:
        return bgr
    scale = min(_MAX_UPSCALE, _SMALL_PLATE_WIDTH / w)
    return cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)


class FastPlateOCR:
    """The global CCT model, via onnxruntime.

    Pinned to the CPU execution provider: onnxruntime's CoreML provider
    mis-partitions this graph on Apple Silicon and throws on every call, which
    presents as ANPR finding nothing rather than as a configuration fault.
    """

    name = "fast-plate-ocr"

    def __init__(self, model: str = "cct-s-v2-global-model") -> None:
        from fast_plate_ocr import LicensePlateRecognizer

        self._m = LicensePlateRecognizer(model, device=os.getenv("SETU_OCR_DEVICE", "cpu"))
        self.model = model
        cfg = self._m.config
        self._grayscale = cfg.image_color_mode == "grayscale"
        self.slots = cfg.max_plate_slots
        if self.slots < 10:
            log.warning(
                "%s has %d character slots; a modern Indian plate is 10 "
                "(GJ01AB1234), so full-length reads will be truncated by "
                "construction.", model, self.slots,
            )

    def read(self, plate_bgr: np.ndarray) -> Read | None:
        img = _upscale_small(plate_bgr)
        arr = (cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if self._grayscale
               else cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        preds = self._m.run(arr, return_confidence=True)
        if not preds:
            return None
        p = preds[0] if isinstance(preds, list) else preds
        probs = getattr(p, "char_probs", None)
        conf = float(sum(probs) / len(probs)) if probs is not None and len(probs) else 0.0
        return Read(_clean(getattr(p, "plate", None)), conf, self.name)


class AwirosOCR:
    """The India-specific PP-OCRv5 model, via PaddlePaddle.

    Needs the PaddleOCR sources for its architecture (``SVTR_HGNet`` /
    ``PPHGNetV2_B4`` / ``MultiHead``) — the pip package does not expose them. The
    model card's own script clones them; this looks for that clone rather than
    performing one, because a worker starting up is the wrong moment to discover
    it needs the network and half a gigabyte of git history.
    """

    name = "awiros-anpr"
    IMAGE_SHAPE = (3, 48, 320)

    def __init__(self, weights: str, dict_path: str, paddleocr_dir: str) -> None:
        if not os.path.isdir(os.path.join(paddleocr_dir, "ppocr")):
            raise RuntimeError(
                f"PaddleOCR sources not found at {paddleocr_dir}. "
                "Clone https://github.com/PaddlePaddle/PaddleOCR there, or run "
                "artifacts/awiros/test.py once, which clones it."
            )
        if paddleocr_dir not in sys.path:
            sys.path.insert(0, paddleocr_dir)

        import paddle
        from ppocr.modeling.architectures import build_model
        from ppocr.postprocess import build_post_process
        from safetensors.numpy import load_file

        paddle.set_device(os.getenv("SETU_OCR_DEVICE", "cpu"))
        self._paddle = paddle

        self._post = build_post_process({
            "name": "CTCLabelDecode",
            "character_dict_path": dict_path,
            "use_space_char": True,
        })
        self._model = build_model({
            "model_type": "rec",
            "algorithm": "SVTR_HGNet",
            "Transform": None,
            "Backbone": {"name": "PPHGNetV2_B4", "text_rec": True},
            "Head": {
                "name": "MultiHead",
                "head_list": [
                    {"CTCHead": {
                        "Neck": {"name": "svtr", "dims": 120, "depth": 2,
                                 "hidden_dims": 120, "kernel_size": [1, 3],
                                 "use_guide": True},
                        "Head": {"fc_decay": 1e-05},
                    }},
                    {"NRTRHead": {"nrtr_dim": 384, "max_text_length": 25}},
                ],
                "out_channels_list": {"CTCLabelDecode": 64, "NRTRLabelDecode": 67},
            },
        })
        self._model.eval()
        self._model.set_state_dict({
            k: paddle.to_tensor(v) for k, v in load_file(weights).items()
        })
        log.info("Loaded India-specific plate OCR from %s", weights)

    def _prepare(self, bgr: np.ndarray) -> np.ndarray:
        """Resize to 48x320 keeping aspect, pad right, normalise to [-1, 1]."""
        c, h, w = self.IMAGE_SHAPE
        ih, iw = bgr.shape[:2]
        ratio = iw / max(ih, 1)
        resized_w = min(w, max(1, int(np.ceil(h * ratio))))
        img = cv2.resize(bgr, (resized_w, h)).astype(np.float32) / 255.0
        img = (img - 0.5) / 0.5
        padded = np.zeros((h, w, c), dtype=np.float32)
        padded[:, :resized_w, :] = img
        return padded.transpose((2, 0, 1))

    def read(self, plate_bgr: np.ndarray) -> Read | None:
        tensor = self._paddle.to_tensor(
            np.expand_dims(self._prepare(plate_bgr), axis=0)
        )
        with self._paddle.no_grad():
            preds = self._model(tensor)
        if isinstance(preds, dict):
            out = preds.get("ctc", next(iter(preds.values())))
        elif isinstance(preds, (list, tuple)):
            out = preds[0]
        else:
            out = preds
        decoded = self._post(out.numpy())
        if not decoded:
            return None
        text, conf = decoded[0]
        return Read(_clean(text), float(conf), self.name)


def build(backend: str, **kw) -> FastPlateOCR | AwirosOCR:
    """Construct a backend by name, falling back loudly rather than silently."""
    if backend == "awiros":
        try:
            return AwirosOCR(**kw)
        except Exception as exc:
            log.error(
                "India-specific OCR unavailable (%s: %s) — falling back to the "
                "global model, which reads this estate's plates at roughly a "
                "third the accuracy.", type(exc).__name__, exc,
            )
            return FastPlateOCR()
    return FastPlateOCR()
