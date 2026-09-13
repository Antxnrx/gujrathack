"""The Setu API: one process in front of the inference layer's database.

Shape of the service
--------------------
Everything an operator does is a read of what the ML pipeline already wrote, an
annotation on it, or a subscription to new ones. There is no inference here — the
worker owns the GPU and the frame loop, and putting model calls behind an HTTP
request would make one slow inference stall every open stream in the control room.
The two halves meet only in Postgres and MinIO.

The one exception is the CLIP *text* encoder in ``clip_text.py``, loaded lazily for
natural-language search. It is here rather than in the worker because a text query
has no frame: it arrives with the request and must be answered synchronously.

Model 2's architectural promise, restated in code
-------------------------------------------------
No departmental video is stored by this platform. ``routers/stream.py`` relays HLS
bytes through without writing them; ``routers/evidence.py`` seals a *manifest* of
references rather than copying clips. What is centralised is metadata — detections,
tracks, plate reads, embeddings and the operator's own annotations — which is what
makes a single search across departments possible without becoming the state's
video archive.

Failure posture
---------------
Degraded, not dead. If MinIO is down, thumbnails come back ``null`` and searches
still answer. If Model 1 is unreachable, ``/api/health`` says so and every endpoint
keeps working. If the LISTEN connection drops, the broker reconnects with backoff
and the SSE streams stay open. The only hard dependency is Postgres, because
without it there is nothing to serve.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from backend.app import bus, db
from backend.app.config import settings
from backend.app.routers import (
    alerts, bookmarks, cameras, detections, dragnet, events, evidence, health,
    incidents, investigate, live, notes, playbooks, search, stream, watchlist, zones,
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("setu.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start what must be running; refuse to start if the essentials are not.

    The pool is created eagerly so a bad ``DATABASE_URL`` fails at boot with one clear
    error rather than turning every request into a 500. The broker is started after it,
    because it needs the same database.

    ``require_auth`` with nothing configured to authenticate against is a startup
    failure rather than a warning: the failure mode it prevents is a deployment that
    believes it is secured and is in fact open.
    """
    if settings.require_auth and not (settings.model1_jwks_url or settings.model1_api_key):
        raise RuntimeError(
            "REQUIRE_AUTH is on but neither MODEL1_JWKS_URL nor MODEL1_API_KEY is set — "
            "there is nothing to verify credentials against"
        )

    await db.connect()
    await bus.broker.start()

    from backend.app.security import auth_mode
    mode = auth_mode()
    if mode == "disabled":
        log.warning(
            "AUTH IS DISABLED — every request is accepted as the local operator. "
            "Set MODEL1_JWKS_URL (and REQUIRE_AUTH=true) before this leaves a laptop."
        )
    log.info("setu-api up: auth=%s, cors=%s", mode, settings.cors_origin_list)

    # Warm the CLIP text encoder in the background. Loading the checkpoint takes about
    # six seconds, and without this the operator who types the first text query of the
    # shift pays all of it — a search box that takes six seconds once and 25 ms
    # afterwards reads as broken, not as cold.
    #
    # Fire-and-forget, and failure is logged rather than raised: this process is
    # supposed to run wherever it can reach Postgres, including boxes with no model
    # checkpoint at all. A missing checkpoint must cost the semantic-ranking half of
    # /search, not the other 59 endpoints.
    async def _warm() -> None:
        from backend.app import clip_text
        try:
            await clip_text.encode("warmup")
            log.info("CLIP text encoder warm: %s", clip_text.describe())
        except Exception as exc:
            log.warning(
                "CLIP text encoder unavailable (%s: %s) — /search still filters and "
                "orders chronologically, but text queries will not rank semantically",
                type(exc).__name__, exc,
            )

    warm_task = asyncio.create_task(_warm())

    try:
        yield
    finally:
        # Order matters: stop accepting new pushes, then close the pool the broker's
        # LISTEN connection and every in-flight query are using.
        # Cancelled first: a half-loaded checkpoint holds a thread the pool teardown
        # below would otherwise wait on.
        warm_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm_task
        await bus.broker.stop()
        from backend.app.routers.stream import aclose as close_relay
        await close_relay()
        await db.disconnect()
        log.info("setu-api down")


app = FastAPI(
    title="Setu API",
    version="1.0.0",
    summary="Unified viewing and search over departmental CCTV metadata",
    description=__doc__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The relay's own headers are set per-response; this exposes the diagnostic one
    # so a browser console can see how many segments a playlist carried.
    expose_headers=["X-Setu-Segments", "X-Response-Time-Ms"],
)

# 1 KB rather than the 500-byte default: search responses are JSON in the tens of
# kilobytes and compress ~8×, while a 300-byte status object costs more in CPU than
# it saves on the wire. Video segments are already compressed and are streamed, so
# GZip leaves them alone.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def timing(request: Request, call_next):
    """Stamp every response with its own duration, and name the slow ones.

    ``X-Response-Time-Ms`` on the response rather than a log line per request: the
    frontend can surface it (a search that took 900 ms is worth knowing about while
    tuning), and it costs one header. Only requests past ``slow_request_ms`` reach the
    log, because a log line per request on a control-room API is a log nobody reads.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
    if elapsed_ms > settings.slow_request_ms:
        log.warning("slow %s %s — %.0f ms", request.method, request.url.path, elapsed_ms)
    return response


# ── Error shape ─────────────────────────────────────────────────────────────
# One JSON shape for every failure, so the frontend has one error path. FastAPI's
# default validation body is a nested list of pydantic internals; a UI cannot show
# that to an operator, and a developer reading it in a console cannot tell which
# field was wrong without decoding it.

@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_failed",
            "detail": [
                {"field": ".".join(str(p) for p in e.get("loc", ())[1:]) or "body",
                 "problem": e.get("msg")}
                for e in exc.errors()
            ],
        },
    )


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback, return a type name.

    The exception's message can carry a DSN, a bucket name or a fragment of SQL. It
    belongs in the log the operator's IT team can read, not in a response body that
    reaches a browser.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "type": type(exc).__name__},
    )


# ── Routers ─────────────────────────────────────────────────────────────────
# Every router is mounted under /api. The prefix is declared once, here, rather than
# repeated in sixteen modules — and routers/stream.py derives its playlist URLs from
# the mounted route, so moving the mount point does not silently break playback.

for module in (
    health,        # /health, /health/live, /health/ready
    cameras,       # feature 2, 4  — camera index, geo/district search
    events,        # feature 1, 3  — event tagging, incident timelines
    detections,    # feature 10-14 — detections, tracks, multi-frame ANPR reads
    search,        # feature 4     — time / location / camera / natural language
    incidents,     # feature 3     — incident timelines
    notes,         # feature 7     — operator notes
    bookmarks,     # feature 6     — event bookmarking
    alerts,        # feature 8     — alerts
    watchlist,     # feature 8     — watchlist that produces them
    zones,         # feature 15-20 — restricted zones, loitering, abandoned objects
    evidence,      # feature 5     — evidence snapshots (manifests, not exports)
    investigate,   # feature 9     — camera-to-camera investigation
    dragnet,       # the reconstruction engine's API
    stream,        # the HLS relay — unified viewer
    live,          # SSE fan-out
    playbooks,     # the console arranged by the job, not by the data model
):
    app.include_router(module.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "setu-api",
        "docs": "/api/docs",
        "health": "/api/health",
        "live": "/api/live",
    }


if __name__ == "__main__":                                  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "backend.app.main:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=bool(os.getenv("API_RELOAD")),
        # One worker. The broker holds a LISTEN connection and the SSE subscribers
        # live in this process's memory; a second worker would have its own set of
        # both, and an operator's stream would only receive the events that happened
        # to be delivered to the worker their connection landed on.
        workers=1,
    )
