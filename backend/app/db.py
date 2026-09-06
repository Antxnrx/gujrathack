"""asyncpg pool, plus the vector coercion every reader needs.

Why asyncpg and not psycopg2
----------------------------
``ml/*`` is synchronous and stays that way — it is a frame loop, and a frame loop
has nothing to await. The API is the opposite shape: mostly waiting on Postgres,
occasionally waiting on the camera gateway, and it has to hold hundreds of open
SSE connections while doing it. One blocking driver call in that design stalls
every other request on the event loop.

The two drivers coexist without conflict; they are separate processes.
"""

from __future__ import annotations

import decimal
import json
import logging
from typing import Any, Optional

import asyncpg

from backend.app.config import settings

log = logging.getLogger("setu.api.db")

_pool: Optional[asyncpg.Pool] = None


def _json_default(value: Any) -> Any:
    """Serialise what asyncpg hands back but ``json`` will not take.

    ``datetime``, ``UUID`` and ``Decimal`` all reach jsonb columns in this schema —
    a manifest carries timestamps, an audit detail carries row ids — and every one of
    them raises from a bare ``json.dumps``. Handling them here rather than in each
    caller is what lets the encoder below be the only serialiser in the backend.
    """
    if hasattr(value, "isoformat"):                 # datetime, date
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return str(value)                               # UUID, IPv4Address, …


def _json_encode(value: Any) -> str:
    return json.dumps(value, default=_json_default)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup.

    jsonb comes back as a string from asyncpg unless a codec is registered, and
    every table in this schema carries at least one jsonb column, so every router
    would otherwise need to remember to ``json.loads``. Registering it once here
    means a forgotten parse is impossible rather than merely unlikely.

    The *encoder* half matters just as much and is easier to get wrong. With a codec
    registered, binding an already-serialised string to a jsonb parameter encodes it
    a second time: Postgres stores a JSON *string* whose content happens to be JSON,
    ``jsonb_typeof`` reports ``string`` rather than ``object``, every ``->>`` against
    it returns NULL, and the value reads back into Python as ``str``. It looks correct
    in a response body and is broken everywhere it is used. So callers pass native
    dicts and lists, and this is the only place that serialises them.

    ``vector`` is registered as text and converted by ``vector_to_list`` below.
    pgvector's binary wire format is a length-prefixed float32 array; decoding it
    by hand is easy to get subtly wrong, and these reads are not hot enough for
    the difference to matter.
    """
    await conn.set_type_codec(
        "jsonb", encoder=_json_encode, decoder=json.loads, schema="pg_catalog", format="text",
    )
    await conn.set_type_codec(
        "json", encoder=_json_encode, decoder=json.loads, schema="pg_catalog", format="text",
    )
    try:
        await conn.set_type_codec(
            "vector", encoder=str, decoder=str, schema="public", format="text",
        )
    except asyncpg.exceptions.UndefinedObjectError:      # extension not installed
        log.warning("pgvector type not found — embedding columns will read as text")


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            init=_init_connection,
            command_timeout=settings.db_statement_timeout_ms / 1000.0,
            server_settings={"application_name": "setu-api"},
        )
        log.info("pg pool up (%d–%d)", settings.db_pool_min, settings.db_pool_max)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.connect() has not run — is this outside the app lifespan?")
    return _pool


# ── Query helpers ───────────────────────────────────────────────────────────
# Thin on purpose. Every router writes its own SQL: the queries here are the
# interesting part of the service, and hiding them behind a query builder would
# hide the indexes they were written against.

async def fetch(sql: str, *args: Any) -> list[dict]:
    async with pool().acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


async def fetchrow(sql: str, *args: Any) -> Optional[dict]:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def fetchval(sql: str, *args: Any) -> Any:
    async with pool().acquire() as conn:
        return await conn.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(sql, *args)


def vector_to_list(value: Any) -> list[float]:
    """Coerce a ``vector`` column into a list of floats, whatever shape it arrived in.

    The same problem as ``ml.analytics.tracks.vector_to_list`` and solved the same
    way, because the API reads the same columns through a different driver: with
    the text codec above a vector arrives as ``'[0.1,0.2,…]'``, without it as
    whatever asyncpg made of the binary format, and psycopg2 on the ML side hands
    back a ``pgvector.Vector`` that is not iterable. Assuming any one of those
    breaks on the others, and the failure is a TypeError deep inside a read.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [float(v) for v in value.strip().strip("[]").split(",") if v.strip()]
    to_list = getattr(value, "to_list", None)          # pgvector.Vector
    if callable(to_list):
        return [float(v) for v in to_list()]
    tolist = getattr(value, "tolist", None)            # numpy ndarray
    if callable(tolist):
        return [float(v) for v in tolist()]
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("vector arrived as binary — the text codec did not register")
    return [float(v) for v in value]


def to_vector_literal(vec: list[float]) -> str:
    """Format a Python list as a pgvector literal for parameter binding.

    asyncpg has no vector encoder, so the value is passed as text and cast in
    SQL: ``$1::vector``. Formatting with repr() rather than str() keeps full
    float32 precision — a truncated embedding silently changes every cosine it
    participates in.
    """
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"
