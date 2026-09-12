"""CLIP text encoding for natural-language search.

Vector-space parity is the whole requirement here. ``ml/pipeline.py`` embeds
crops with open_clip ``ViT-B-32`` / ``pretrained="openai"``; a text query encoded
with any other checkpoint lands in a different space, and the failure mode is not
an error — it is a plausible-looking ranking that is silently no better than
random. So the model identifiers come from settings and default to exactly what
the pipeline uses, and ``/api/health`` reports which checkpoint is loaded so a
mismatch is visible rather than inferred from bad results.

The encoder is loaded lazily on the first text search and then kept. Loading it
at startup would add ~4 s and ~350 MB to a service that may never receive a text
query — an operator scrubbing camera timelines never touches this path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

from backend.app.config import settings

log = logging.getLogger("setu.api.clip")

_model = None
_tokenizer = None
_device = "cpu"
_lock = threading.Lock()

# ── Structured hints inside a free-text query ───────────────────────────────
# "white truck near Adalaj yesterday" carries three different kinds of
# constraint, and only one of them is a job for CLIP. Colour and class are exact
# columns in the DB — filtering on them is both faster and more accurate than
# hoping the embedding captures them, and it lets the vector search do the part
# it is actually good at (everything the words did not pin down).
#
# The extracted terms are *removed* from the text before encoding, because
# leaving them in makes the embedding redundant with the filter rather than
# complementary to it.
_COLOR_WORDS = {
    "red": "red", "blue": "blue", "white": "white", "yellow": "yellow",
    "black": "black", "silver": "silver", "grey": "silver", "gray": "silver",
    "green": "green", "orange": "orange",
}
_CLASS_WORDS = {
    "car": "car", "sedan": "car", "suv": "car", "taxi": "car",
    "motorcycle": "motorcycle", "motorbike": "motorcycle", "bike": "motorcycle",
    "scooter": "motorcycle", "activa": "motorcycle",
    "auto": "auto_rickshaw", "rickshaw": "auto_rickshaw", "autorickshaw": "auto_rickshaw",
    "tuktuk": "auto_rickshaw",
    "bus": "bus", "coach": "bus",
    "truck": "truck", "lorry": "truck", "tempo": "truck", "van": "truck",
    "tractor": "tractor",
    "person": "person", "people": "person", "pedestrian": "person", "man": "person",
    "woman": "person", "child": "person",
    "bag": "backpack", "backpack": "backpack", "suitcase": "suitcase",
    "luggage": "suitcase", "handbag": "handbag",
}


def parse_hints(text: str) -> dict:
    """Pull colour and class out of a query. Returns the residual text too."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    colors = {_COLOR_WORDS[t] for t in tokens if t in _COLOR_WORDS}
    classes = {_CLASS_WORDS[t] for t in tokens if t in _CLASS_WORDS}
    residual = " ".join(
        t for t in tokens if t not in _COLOR_WORDS and t not in _CLASS_WORDS
    )
    return {
        "colors": sorted(colors),
        "classes": sorted(classes),
        "residual": residual.strip(),
        "consumed": bool(colors or classes),
    }


def _load() -> None:
    global _model, _tokenizer, _device
    if _model is not None:
        return
    with _lock:
        if _model is not None:
            return
        import open_clip
        import torch

        _device = (
            "cuda" if torch.cuda.is_available()
            else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
            else "cpu"
        )
        model, _, _ = open_clip.create_model_and_transforms(
            settings.clip_model_name, pretrained=settings.clip_pretrained,
        )
        model = model.to(_device).eval()
        _tokenizer = open_clip.get_tokenizer(settings.clip_model_name)
        _model = model
        log.info(
            "CLIP text encoder ready: %s/%s on %s",
            settings.clip_model_name, settings.clip_pretrained, _device,
        )


def _encode_sync(text: str) -> list[float]:
    import torch

    _load()
    with torch.no_grad():
        tokens = _tokenizer([text]).to(_device)
        emb = _model.encode_text(tokens)
        emb = emb / emb.norm(dim=-1, keepdim=True)
    return [float(v) for v in emb[0].cpu().tolist()]


async def encode(text: str) -> list[float]:
    """Encode a query, off the event loop.

    The first call loads the checkpoint (seconds); later calls are milliseconds
    of matrix multiply. Both are blocking, and blocking the loop here would stall
    every open SSE stream in the control room.
    """
    return await asyncio.to_thread(_encode_sync, text)


def loaded() -> bool:
    return _model is not None


def describe() -> dict:
    return {
        "model": settings.clip_model_name,
        "pretrained": settings.clip_pretrained,
        "device": _device if _model is not None else None,
        "loaded": _model is not None,
        # Divergence from the worker is now prevented rather than detected: both
        # halves read CLIP_MODEL, so they cannot be pointed at different
        # checkpoints. What remains worth checking is the pairing itself. OpenAI's
        # CLIP uses QuickGELU, and open_clip only applies it when the architecture
        # name says so — asking for "ViT-B-32" with pretrained="openai" loads the
        # right weights into the wrong activation and returns embeddings that are
        # subtly and silently wrong. open_clip warns; nothing fails.
        "matches_pipeline": (
            settings.clip_pretrained != "openai"
            or settings.clip_model_name.endswith("-quickgelu")
        ),
        "shared_env_var": "CLIP_MODEL",
    }


def cosine(a: list[float], b: list[float]) -> float | None:
    """Cosine of two already-L2-normalised vectors. None if either is empty."""
    if not a or not b or len(a) != len(b):
        return None
    return sum(x * y for x, y in zip(a, b))
