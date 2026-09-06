"""The live channel: one SSE stream carrying everything that happens.

One stream, not one per feature. A control-room console shows alerts, event
transitions and pursuit activity on the same screen, and three EventSources means
three TCP connections, three keepalive timers and three chances for one of them to
be the stale one. The client subscribes to topics it wants and filters the rest.

The camera filter is the reason this matters for latency. A wall showing four feeds
asks for four camera ids and receives only their traffic; without it every console
in the state receives every detection-derived alert and drops most of them in
JavaScript, which is CPU spent on the operator's machine to achieve nothing.

Back-pressure is handled in ``bus.Subscription.offer``: a full queue drops the
*oldest* message and emits a ``lag`` frame. A control room would rather know it
missed something and see the present than receive a faithful replay of the past.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from backend.app import bus, sse
from backend.app.config import settings
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.live")
router = APIRouter(tags=["live"])


@router.get("/live")
async def live(
    request: Request,
    topics: Optional[str] = Query(default=None, description="Comma-separated: setu_events, setu_alerts, setu_pursuits"),
    cameras: Optional[str] = Query(default=None, description="Comma-separated camera UUIDs"),
    operator: Operator = Depends(current_operator),
):
    wanted = [t.strip() for t in (topics or "").split(",") if t.strip()] or None
    if wanted:
        unknown = [t for t in wanted if t not in bus.CHANNELS]
        if unknown:
            # Silently accepting an unknown topic gives an operator a stream that
            # works, receives nothing, and looks like a quiet night.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown topic(s) {unknown}; available: {list(bus.CHANNELS)}",
            )
    cam_list = [c.strip() for c in (cameras or "").split(",") if c.strip()] or None

    # The subscription is entered *inside* the generator, not around the return.
    # A `with` around `return StreamingResponse(...)` exits the moment the handler
    # returns — before a single byte of the body is produced — which unregisters the
    # subscriber and yields a stream that connects, keepalives forever and delivers
    # nothing. Holding it in the generator ties its lifetime to the response body,
    # and Starlette closing the generator on disconnect is what unregisters it.
    async def body():
        with bus.broker.subscribe(topics=wanted, cameras=cam_list) as sub:
            hello = {
                "operator": operator.label,
                "topics": sorted(sub.topics),
                "cameras": sorted(sub.cameras) if sub.cameras else None,
                "keepalive_s": settings.sse_keepalive_seconds,
                "bus": bus.broker.describe(),
            }
            async for chunk in sse.stream(request, sub, hello=hello):
                yield chunk

    return StreamingResponse(body(), media_type="text/event-stream", headers=sse.SSE_HEADERS)


@router.get("/live/status")
async def status_(_: Operator = Depends(current_operator)) -> dict:
    return bus.broker.describe()
