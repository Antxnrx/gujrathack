"""Server-Sent Events framing.

SSE rather than WebSockets, deliberately. Everything the control room pushes goes
one way — the browser never sends anything back over this channel — and SSE is
plain HTTP: it survives corporate proxies that break WebSocket upgrades, it
reconnects on its own with no client-side retry logic, and it needs no second
protocol in the stack. A government network is exactly where the upgrade handshake
turns out to be blocked.

The keepalive comment is not optional. An idle SSE response with no bytes in flight
looks like a hung connection to every intermediary between here and the operator,
and something in that chain will close it after 30–60 s. A ``:`` comment line is
ignored by the EventSource parser and costs two bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from backend.app.bus import Subscription
from backend.app.config import settings

log = logging.getLogger("setu.api.sse")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx buffers proxied responses by default, which turns a live stream into
    # a stream that arrives in 4 KB batches — i.e. not a live stream.
    "X-Accel-Buffering": "no",
}


def frame(data: dict, event: Optional[str] = None, event_id: Optional[str] = None) -> str:
    out = []
    if event:
        out.append(f"event: {event}")
    if event_id:
        out.append(f"id: {event_id}")
    out.append("data: " + json.dumps(data, separators=(",", ":")))
    return "\n".join(out) + "\n\n"


async def stream(
    request: Request,
    sub: Subscription,
    hello: Optional[dict] = None,
) -> AsyncIterator[str]:
    """Yield frames until the client goes away.

    ``request.is_disconnected()`` is checked on every keepalive rather than only
    when a message arrives: on a quiet night a closed tab would otherwise hold a
    subscription until the next event, which on a quiet night is a long time.
    """
    yield frame(hello or {"ok": True}, event="hello")
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    sub.queue.get(), timeout=settings.sse_keepalive_seconds
                )
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                continue
            yield frame(message["data"], event=message["topic"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:                      # pragma: no cover — defensive
        log.warning("stream ended abnormally: %s", exc)
        with contextlib.suppress(Exception):
            yield frame({"error": type(exc).__name__}, event="error")


def response(request: Request, sub: Subscription, hello: Optional[dict] = None) -> StreamingResponse:
    return StreamingResponse(
        stream(request, sub, hello),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
