"""``/api/health`` — one request that says whether the control room is usable.

Written to be diagnostic rather than binary. A single ``{"ok": false}`` tells an
operator nothing they can act on, and in this system most dependencies are
degradable: MinIO down means no thumbnails but search still works, Model 1 down
means no fresh camera directory but the cameras already known still stream, CLIP
not loaded is the normal state until someone runs a text search. So each
dependency reports its own state and the top-level ``status`` distinguishes
``ok`` from ``degraded`` from ``down``, where only Postgres can produce ``down``.

``clip.matches_pipeline`` is here for a specific silent failure: a text query
encoded with a different checkpoint than ``ml/pipeline.py`` used for the crops
returns a ranking that looks plausible and is worthless. That mismatch has no
error to raise, so it is surfaced as a fact about the running process.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from backend.app import bus, clip_text, db, storage
from backend.app.config import settings
from backend.app.security import auth_mode, model1_health

router = APIRouter(tags=["health"])
_started = time.time()


@router.get("/health")
async def health() -> dict:
    checks: dict = {}

    try:
        t0 = time.perf_counter()
        one = await db.fetchval("SELECT 1")
        checks["postgres"] = {
            "reachable": one == 1,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    except Exception as exc:
        checks["postgres"] = {"reachable": False, "error": type(exc).__name__}

    if checks["postgres"].get("reachable"):
        try:
            counts = await db.fetchrow(
                """
                SELECT (SELECT COUNT(*) FROM cameras)   AS cameras,
                       (SELECT COUNT(*) FROM detections) AS detections,
                       (SELECT COUNT(*) FROM tracks)     AS tracks,
                       (SELECT COUNT(*) FROM events)     AS events,
                       (SELECT COUNT(*) FROM events WHERE status = 'new') AS open_events,
                       (SELECT COUNT(*) FROM alerts)     AS alerts,
                       (SELECT COUNT(*) FROM incidents WHERE status <> 'closed') AS open_incidents,
                       (SELECT COUNT(*) FROM pursuits WHERE status = 'active')   AS active_pursuits,
                       (SELECT MAX(ts) FROM detections)  AS last_detection_at
                """
            )
            checks["data"] = counts
        except Exception as exc:
            checks["data"] = {"error": type(exc).__name__}

    checks["minio"] = storage.health()
    checks["model1"] = await model1_health()
    checks["clip"] = clip_text.describe()
    checks["bus"] = bus.broker.describe()
    checks["auth"] = auth_mode()

    down = not checks["postgres"].get("reachable")
    degraded = (
        not checks["minio"].get("reachable")
        or checks["model1"].get("reachable") is False
        or not checks["bus"].get("connected")
    )
    return {
        "status": "down" if down else "degraded" if degraded else "ok",
        "uptime_s": round(time.time() - _started, 1),
        "presign_expiry_s": settings.presign_expiry_seconds,
        "checks": checks,
    }


@router.get("/health/live")
async def live() -> dict:
    """Liveness only — is the process answering? No dependency touched.

    Kept separate from /health on purpose: an orchestrator restarting the API
    because MinIO is unreachable would take the control room down over a missing
    thumbnail.
    """
    return {"alive": True}


@router.get("/health/ready")
async def ready() -> dict:
    try:
        await db.fetchval("SELECT 1")
        return {"ready": True}
    except Exception as exc:
        return {"ready": False, "error": type(exc).__name__}
