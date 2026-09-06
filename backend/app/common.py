"""Helpers shared by the routers: audit trail, paging, camera resolution.

The audit trail is the reason this module exists. In a police deployment "who
looked at what, and who changed the status of this event" is not a nice-to-have —
it is the difference between evidence and an unattributed database row. Every
mutation writes an ``audit_log`` entry with the operator resolved by
``security.current_operator``, and it is written in the same request, not queued,
because an audit trail that can be lost is not one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, Query, status

from backend.app import db
from backend.app.security import Operator

log = logging.getLogger("setu.api")


async def audit(operator: Operator, action: str, target: Optional[str] = None, **detail: Any) -> None:
    try:
        await db.execute(
            "INSERT INTO audit_log (operator, action, target, detail) VALUES ($1, $2, $3, $4::jsonb)",
            # `detail`, not json.dumps(detail): db.py's jsonb codec serialises. Passing
            # a string here would store a JSON string, and `detail->>'x'` in any
            # later audit query would return NULL.
            operator.subject, action, target, detail,
        )
    except Exception as exc:
        # A failed audit write must not fail the operator's action — but it must
        # be loud, because a silent gap in the trail is worse than a noisy one.
        log.error("AUDIT WRITE FAILED action=%s target=%s: %s", action, target, exc)


def envelope(items: list[dict], limit: int, offset: int, total: Optional[int] = None) -> dict:
    """Uniform list response.

    ``total`` is optional because counting is not always cheap. Where a count needs
    its own scan over a table the UI would render identically without, the routers
    return ``has_more`` instead — derived by asking for one row more than the page
    and discarding it.
    """
    out = {"items": items, "limit": limit, "offset": offset, "count": len(items)}
    if total is not None:
        out["total"] = total
    return out


def paginate(rows: list[dict], limit: int, offset: int) -> dict:
    """Trim a limit+1 fetch back to the page and report whether more exist."""
    has_more = len(rows) > limit
    page = rows[:limit]
    body = envelope(page, limit, offset)
    body["has_more"] = has_more
    return body


def window(
    from_ts: Optional[datetime] = Query(default=None, description="ISO-8601 lower bound (inclusive)"),
    to_ts: Optional[datetime] = Query(default=None, description="ISO-8601 upper bound (exclusive)"),
    last_minutes: Optional[int] = Query(default=None, ge=1, le=60 * 24 * 30),
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Resolve a time window from either explicit bounds or a relative shorthand.

    ``last_minutes`` exists because the query an operator actually types is "the
    last twenty minutes", and making the UI compute two timestamps for that is how
    clock-skew bugs get in.

    Naive datetimes are assumed UTC. Every timestamp in this schema is
    ``TIMESTAMPTZ`` and every writer uses UTC, so treating a naive value as local
    time would shift results by the deployment's offset — five and a half hours,
    in Gujarat.
    """
    if last_minutes is not None:
        now = datetime.now(timezone.utc)
        return now - timedelta(minutes=last_minutes), None
    return _utc(from_ts), _utc(to_ts)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def utc(value: Optional[datetime]) -> Optional[datetime]:
    return _utc(value)


async def resolve_camera(ref: str) -> dict:
    """Accept either a camera UUID or its ``external_id`` (``cam07``).

    The ML layer keys everything on ``external_id`` — it is what appears in
    thumbnail object keys, seed configs and logs — while the schema's foreign keys
    are UUIDs. Making every URL accept both means an operator can paste either one
    out of whatever they were looking at.
    """
    row = await db.fetchrow(
        """
        SELECT * FROM cameras
        WHERE external_id = $1
           OR camera_uid = $1
           OR (($1 ~ '^[0-9a-fA-F-]{36}$') AND id = $1::uuid)
        LIMIT 1
        """,
        ref,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No camera '{ref}'")
    return row


def as_uuid(value: str, what: str = "id") -> str:
    """Reject anything that is not a UUID before it reaches a ``::uuid`` cast.

    Not for safety — every query is parameterised — but for the response code. A
    malformed id is a 400, and without this check Postgres raises and FastAPI
    turns it into a 500, which says "we broke" instead of "you did".
    """
    import uuid as _uuid
    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Malformed {what}: {value!r}")
