"""Postgres LISTEN/NOTIFY fanned out to SSE subscribers.

Why LISTEN and not polling
--------------------------
The control room's whole value is that an operator sees a wrong-way vehicle while
it is still on the road. Polling ``events`` every second gives a one-second floor
on that latency and costs a query per second per open tab, forever, whether or not
anything happened. ``NOTIFY`` is pushed by the same transaction that wrote the
row: the trigger fires on commit and the operator's browser has it in single-digit
milliseconds, with the database doing no work at all while the road is quiet.

Three channels exist, written by triggers, not by this process:

* ``setu_events``   — JSON: ``id, camera_id, event_type, severity, started_at``
* ``setu_pursuits`` — JSON: ``pursuit_id, sighting_id, camera_id, ts, score, match_kind``
* ``setu_alerts``   — a bare alert UUID (``scripts/schema.sql`` predates the others)

One connection, not one per subscriber
--------------------------------------
``LISTEN`` is connection-scoped, so the naive design gives every open browser tab
its own Postgres backend. A control room with a video wall and a dozen operators
would then hold fifty idle connections that exist only to be woken up. Instead one
dedicated connection — held outside the pool, because a pooled connection gets
handed to the next query and its ``LISTEN`` registration goes with it — receives
every notification and this module copies it into each subscriber's queue.

Slow subscribers are dropped from, never waited for
---------------------------------------------------
A subscriber is a browser on a possibly-terrible connection. If a queue is full,
the oldest item is discarded and a ``lag`` marker is sent, because for live
monitoring the newest event is the one that matters and the operator needs to know
they missed some. The alternative — awaiting the slow queue — would let one stalled
tab hold up delivery to every other operator in the building.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

from backend.app import db
from backend.app.config import settings

log = logging.getLogger("setu.api.bus")

EVENTS = "setu_events"
PURSUITS = "setu_pursuits"
ALERTS = "setu_alerts"
CHANNELS = (EVENTS, PURSUITS, ALERTS)


# `eq=False` is load-bearing, not style. The broker keeps subscribers in a set and
# does `add`/`discard` on the live object, so a Subscription has to be hashable — and
# the default dataclass `__eq__` sets `__hash__ = None`, which made every single
# `/api/live` connection raise `TypeError: unhashable type: 'Subscription'` before it
# could yield a byte. `eq=False` restores identity hashing, which is also the only
# correct semantics here: two operators who opened the same topics with the same camera
# filter are two subscribers with two queues, not one, and value equality would have
# had the second one's `discard` evict the first one's stream. `frozen=True` would also
# hash, and is wrong for the same reason it looks tidy — `offer()` mutates `dropped`.
@dataclass(eq=False)
class Subscription:
    """One SSE stream's view of the firehose."""

    topics: frozenset[str]
    cameras: Optional[frozenset[str]]
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=settings.sse_queue_size))
    dropped: int = 0

    def wants(self, topic: str, payload: dict) -> bool:
        if topic not in self.topics:
            return False
        if self.cameras is None:
            return True
        cam = payload.get("camera_id")
        # A message with no camera (a pursuit-level update) is never filtered out
        # by a camera filter — the operator asked to watch cameras, not to stop
        # hearing about the pursuit those cameras belong to.
        return cam is None or str(cam) in self.cameras

    def offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(
                    {"topic": "lag", "data": {"dropped": self.dropped}, "ts": time.time()}
                )


class Broker:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._conn: Optional[asyncpg.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()
        self._terminated: Optional[asyncio.Event] = None
        self.received = 0
        self.connected_since: Optional[float] = None
        self.reconnects = 0

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            self._closed.clear()
            self._task = asyncio.create_task(self._supervise(), name="setu-bus")

    async def stop(self) -> None:
        self._closed.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._drop_connection()

    async def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close(timeout=2)
            self._conn = None
            self.connected_since = None

    async def _supervise(self) -> None:
        """Hold the listening connection open, reconnecting with backoff.

        A dropped listener is silent — no error reaches any request, the events
        simply stop arriving — which makes it exactly the failure worth being
        noisy about in the log and reporting through /api/health.
        """
        backoff = 1.0
        while not self._closed.is_set():
            try:
                await self._connect()
                backoff = 1.0
                await self._terminated.wait()          # type: ignore[union-attr]
                if not self._closed.is_set():
                    log.warning("listener connection terminated — reconnecting")
                    self.reconnects += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("listener unavailable (%s) — retry in %.0fs", exc, backoff)
            finally:
                await self._drop_connection()
            if self._closed.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect(self) -> None:
        self._terminated = asyncio.Event()
        terminated = self._terminated
        conn = await asyncpg.connect(dsn=settings.database_url)
        conn.add_termination_listener(lambda _c: terminated.set())
        for channel in CHANNELS:
            await conn.add_listener(channel, self._on_notify)
        self._conn = conn
        self.connected_since = time.time()
        log.info("listening on %s", ", ".join(CHANNELS))

    # ── receive ─────────────────────────────────────────────────────────────

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        """asyncpg calls this on the event loop, synchronously. Do no I/O here."""
        self.received += 1
        if channel == ALERTS:
            # Bare UUID: it needs a row read to become anything an operator can
            # see, and a read cannot happen in this callback.
            asyncio.create_task(self._hydrate_alert(payload.strip()))
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("undecodable payload on %s", channel)
            return
        self.publish(channel, data)

    async def _hydrate_alert(self, alert_id: str) -> None:
        """Turn an alert id into a usable message — or suppress it as a duplicate.

        ``tr_alert_mirror`` copies watchlist and ANPR alerts into ``events``, and
        that insert fires ``setu_events`` for the same real-world thing. Emitting
        both would show the operator one hit twice. Rather than duplicating the
        trigger's list of mirrored types here — two copies of one rule, free to
        drift — this asks the database what it actually did: if an event points at
        this alert, the event notification is the canonical one and this is noise.
        """
        try:
            row = await db.fetchrow(
                """
                SELECT a.id, a.alert_type, a.status, a.created_at,
                       d.camera_id, d.plate_text, d.class, d.thumbnail_key, d.ts,
                       c.external_id AS camera_external_id, c.name AS camera_name,
                       w.value AS watchlist_value, w.reason AS watchlist_reason,
                       EXISTS (SELECT 1 FROM events e WHERE e.alert_id = a.id) AS mirrored
                FROM alerts a
                JOIN detections d ON d.id = a.detection_id
                LEFT JOIN cameras c ON c.id = d.camera_id
                LEFT JOIN watchlist_entries w ON w.id = a.watchlist_entry_id
                WHERE a.id = $1::uuid
                """,
                alert_id,
            )
        except Exception as exc:
            log.warning("alert %s could not be hydrated: %s", alert_id, exc)
            return
        if row is None or row.pop("mirrored", False):
            return
        self.publish(ALERTS, {k: (str(v) if k.endswith("_id") else v) for k, v in row.items()})

    def publish(self, topic: str, data: dict) -> None:
        """Fan one message out. Also the entry point for API-originated messages."""
        message = {"topic": topic, "data": _jsonable(data), "ts": time.time()}
        for sub in list(self._subs):
            if sub.wants(topic, data):
                sub.offer(message)

    # ── subscribe ───────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def subscribe(
        self,
        topics: Optional[list[str]] = None,
        cameras: Optional[list[str]] = None,
    ):
        sub = Subscription(
            topics=frozenset(topics or CHANNELS),
            cameras=frozenset(str(c) for c in cameras) if cameras else None,
        )
        self._subs.add(sub)
        log.debug("subscriber joined (%d total)", len(self._subs))
        try:
            yield sub
        finally:
            self._subs.discard(sub)
            log.debug("subscriber left (%d total)", len(self._subs))

    def describe(self) -> dict:
        return {
            "connected": self._conn is not None,
            "channels": list(CHANNELS),
            "subscribers": len(self._subs),
            "received": self.received,
            "reconnects": self.reconnects,
            "uptime_s": round(time.time() - self.connected_since, 1) if self.connected_since else None,
        }


def _jsonable(value: Any) -> Any:
    """Make a row safe for ``json.dumps`` without importing a serialiser.

    asyncpg hands back ``datetime``, ``UUID`` and ``Decimal``; only the first two
    reach here, and both are unambiguous as strings.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


broker = Broker()
