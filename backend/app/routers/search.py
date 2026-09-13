"""Feature 4 — one search endpoint for time, location, camera, plate and description.

The design decision worth stating: this is **one** endpoint, not five. An operator
does not know in advance whether the thing they remember is a plate fragment, a
colour, a place or a phrase — usually it is several at once ("white truck near
Adalaj around 3pm, plate ended 394"). Splitting that into /search/plate,
/search/text and /search/location would push the job of deciding which one to call
into the UI, and the UI does not know either.

How the filters compose
-----------------------
Everything structured becomes a SQL predicate; only what is left over goes to the
vector index. ``clip_text.parse_hints`` pulls colour and class words out of the
free text first, because those are indexed columns — filtering on
``dominant_color = 'white'`` is exact and index-backed, while hoping a CLIP
embedding encodes "white" is neither. The residual words are what the embedding is
actually good at.

Plate search is two-stage on purpose. An exact match on the consensus plate is the
answer when it exists; when it does not, a trigram similarity search over
``plate_text`` finds the reads that are one OCR confusion away, and those are
returned *marked* as fuzzy rather than mixed in silently. ``scripts/score_plate_truth.py``
measured against fifteen plates read by eye, the system is exactly right 67% of
the time and one character out in most of the rest,
so a search that only ever did exact matching would miss most of the fleet.

Track level by default
----------------------
A track is one vehicle passing one camera; a detection is one frame of it. An
operator searching for a white truck wants a list of trucks, not 40 frames of the
same truck. ``level="detection"`` exists for when the frame itself is the subject —
picking the sharpest crop for evidence, say.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from backend.app import clip_text, db, storage
from backend.app.common import paginate, utc
from backend.app.config import settings
from backend.app.schemas import SearchRequest
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.search")
router = APIRouter(prefix="/search", tags=["search"])

# Below this trigram similarity a "fuzzy plate match" is noise. 0.45 keeps
# one- and two-character differences on a 10-character plate and drops the rest;
# pg_trgm's default 0.3 returns half the fleet for a short query.
_PLATE_SIMILARITY_FLOOR = 0.45


class _Where:
    """Accumulates predicates and their bound parameters in step.

    A list of strings plus a list of values is the whole abstraction. It exists so
    that adding a filter cannot desynchronise the ``$n`` placeholders from the
    argument list — the failure mode of building this by hand is a query that binds
    the district into the confidence comparison.
    """

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.args: list[Any] = []

    def add(self, template: str, *values: Any) -> None:
        rendered = template
        for value in values:
            self.args.append(value)
            rendered = rendered.replace("?", f"${len(self.args)}", 1)
        self.clauses.append(rendered)

    def param(self, value: Any) -> str:
        self.args.append(value)
        return f"${len(self.args)}"

    def sql(self, prefix: str = "WHERE") -> str:
        return f"{prefix} " + " AND ".join(self.clauses) if self.clauses else ""


async def _camera_scope(req: SearchRequest, w: _Where, alias: str) -> None:
    """Turn camera ids, districts and a radius into predicates on ``alias``.

    Location filtering resolves to a set of camera ids rather than joining and
    filtering geographically on every row: cameras are the only thing in this schema
    that has a position, there are tens of them, and a detection's location *is* its
    camera's. One small query up front beats a distance computation per detection.
    """
    refs = [str(c) for c in req.camera_ids]
    ids: Optional[list[str]] = None

    if refs:
        rows = await db.fetch(
            "SELECT id FROM cameras WHERE external_id = ANY($1::text[]) OR camera_uid = ANY($1::text[]) "
            "OR id::text = ANY($1::text[])",
            refs,
        )
        ids = [str(r["id"]) for r in rows]
        if not ids:
            ids = ["00000000-0000-0000-0000-000000000000"]   # matches nothing, honestly

    if req.near and req.radius_km:
        lat, lng = req.near
        near_rows = await db.fetch(
            """
            SELECT id FROM cameras
            WHERE lat IS NOT NULL AND lng IS NOT NULL
              AND 6371 * 2 * asin(sqrt(
                    power(sin(radians($1 - lat) / 2), 2) +
                    cos(radians($1)) * cos(radians(lat)) *
                    power(sin(radians($2 - lng) / 2), 2)
                  )) <= $3
            """,
            lat, lng, req.radius_km,
        )
        near_ids = [str(r["id"]) for r in near_rows] or ["00000000-0000-0000-0000-000000000000"]
        ids = near_ids if ids is None else [i for i in ids if i in set(near_ids)] or \
            ["00000000-0000-0000-0000-000000000000"]

    if ids is not None:
        w.add(f"{alias}.camera_id = ANY(?::uuid[])", ids)
    if req.districts:
        w.add(
            f"{alias}.camera_id IN (SELECT id FROM cameras WHERE district = ANY(?::text[]))",
            list(req.districts),
        )


@router.post("")
async def search(
    req: SearchRequest,
    _: Operator = Depends(current_operator),
) -> dict:
    started = time.perf_counter()
    table = "tracks" if req.level == "track" else "detections"
    a = "t" if req.level == "track" else "d"
    time_col = "first_seen" if req.level == "track" else "ts"

    hints = clip_text.parse_hints(req.q or "")
    classes = list({*req.classes, *hints["classes"]})
    colors = list({*req.colors, *hints["colors"]})

    w = _Where()
    await _camera_scope(req, w, a)

    if req.from_ts:
        w.add(f"{a}.{time_col} >= ?", utc(req.from_ts))
    if req.to_ts:
        w.add(f"{a}.{time_col} < ?", utc(req.to_ts))
    if classes:
        w.add(f"{a}.class = ANY(?::text[])", classes)
    if colors:
        w.add(f"{a}.dominant_color = ANY(?::text[])", colors)
    if req.min_plate_confidence is not None:
        w.add(f"{a}.plate_confidence >= ?", req.min_plate_confidence)

    plate_mode = "none"
    if req.plate:
        exact = await db.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {table} {a} WHERE {a}.plate_text = $1)", req.plate
        )
        if exact:
            plate_mode = "exact"
            w.add(f"{a}.plate_text = ?", req.plate)
        else:
            # Fall back to trigram similarity rather than returning nothing: the
            # plate an operator half-remembers and the plate OCR half-read are the
            # same kind of error, and both are recoverable here.
            plate_mode = "fuzzy"
            w.add(
                f"{a}.plate_text IS NOT NULL AND similarity({a}.plate_text, ?) >= ?",
                req.plate, _PLATE_SIMILARITY_FLOOR,
            )

    # ── ordering: semantic when there are words left, chronological otherwise ──
    vector_used = False
    order_sql = f"{a}.{time_col} DESC"
    select_extra = ""
    residual = hints["residual"] if hints["consumed"] else (req.q or "").strip()
    if residual and not req.plate:
        vec = await clip_text.encode(residual)
        literal = w.param(db.to_vector_literal(vec))
        w.clauses.append(f"{a}.embedding IS NOT NULL")
        # 1 - cosine distance, so the number in the response rises with relevance.
        select_extra = f", 1 - ({a}.embedding <=> {literal}::vector) AS similarity"
        order_sql = f"{a}.embedding <=> {literal}::vector"
        vector_used = True
    elif req.plate and plate_mode == "fuzzy":
        sim = w.param(req.plate)
        select_extra = f", similarity({a}.plate_text, {sim}) AS plate_similarity"
        order_sql = f"similarity({a}.plate_text, {sim}) DESC, {a}.{time_col} DESC"

    if req.level == "track":
        columns = f"""
            t.id, t.camera_id, t.class, t.dominant_color, t.plate_text, t.plate_confidence,
            t.plate_read_count, t.first_seen, t.last_seen, t.first_pts_ms, t.last_pts_ms,
            t.duration_s, t.detection_count, t.thumbnail_key, t.heading_deg,
            t.displacement_norm, t.best_detection_id, t.source{select_extra}
        """
    else:
        columns = f"""
            d.id, d.camera_id, d.class, d.dominant_color, d.plate_text, d.plate_confidence,
            d.confidence, d.ts, d.pts_ms, d.bbox, d.thumbnail_key, d.track_uuid,
            d.frame_w, d.frame_h{select_extra}
        """

    limit = min(req.limit, settings.search_max_limit)
    sql = f"""
        SELECT {columns},
               c.external_id AS camera_external_id, c.name AS camera_name,
               c.district, c.area, c.lat, c.lng
        FROM {table} {a}
        JOIN cameras c ON c.id = {a}.camera_id
        {w.sql()}
        ORDER BY {order_sql}
        LIMIT {w.param(limit + 1)} OFFSET {w.param(req.offset)}
    """
    rows = await db.fetch(sql, *w.args)
    body = paginate(rows, limit, req.offset)
    await storage.attach_thumbnails(body["items"])

    storage.attach_plate_trust(body["items"])

    body["query"] = {
        "level": req.level,
        "plate_mode": plate_mode,
        "vector_search": vector_used,
        "residual_text": residual if vector_used else None,
        "classes": classes,
        "colors": colors,
        "hints_extracted": hints["consumed"],
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    return body


@router.get("")
async def search_get(
    q: Optional[str] = None,
    plate: Optional[str] = None,
    camera: Optional[str] = Query(default=None, description="repeatable camera id or external_id"),
    district: Optional[str] = None,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    level: str = "track",
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    operator: Operator = Depends(current_operator),
) -> dict:
    """Query-string form of the same search, for links an operator can paste.

    A saved search has to survive being copied into a chat window, and a POST body
    cannot be a URL.
    """
    from datetime import datetime

    req = SearchRequest(
        q=q, plate=plate,
        camera_ids=[camera] if camera else [],
        districts=[district] if district else [],
        from_ts=datetime.fromisoformat(from_ts) if from_ts else None,
        to_ts=datetime.fromisoformat(to_ts) if to_ts else None,
        level="detection" if level == "detection" else "track",
        limit=limit, offset=offset,
    )
    return await search(req, operator)


@router.get("/suggest")
async def suggest(
    prefix: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=10, ge=1, le=50),
    _: Operator = Depends(current_operator),
) -> dict:
    """Plate autocomplete from what has actually been read.

    Suggesting from ``tracks`` rather than ``detections`` means the suggestions are
    consensus plates — the 85%-accurate ones — instead of every noisy single-frame
    read.
    """
    normalised = "".join(ch for ch in prefix.upper() if ch.isalnum())
    if not normalised:
        return {"plates": []}
    rows = await db.fetch(
        """
        SELECT t.plate_text, MAX(t.plate_confidence) AS confidence,
               COUNT(*) AS sightings, MAX(t.last_seen) AS last_seen,
               COUNT(DISTINCT t.camera_id) AS cameras
        FROM tracks t
        WHERE t.plate_text IS NOT NULL AND t.plate_text LIKE $1 || '%'
        GROUP BY t.plate_text
        ORDER BY MAX(t.last_seen) DESC
        LIMIT $2
        """,
        normalised, limit,
    )
    return {"plates": rows, "prefix": normalised}


@router.get("/facets")
async def facets(_: Operator = Depends(current_operator)) -> dict:
    """The values a filter UI should offer, taken from the data rather than a constant.

    Hardcoding the class list in the frontend means a new YOLO class silently never
    appears as an option.
    """
    classes = await db.fetch(
        "SELECT class AS value, COUNT(*) AS n FROM tracks WHERE class IS NOT NULL "
        "GROUP BY class ORDER BY n DESC"
    )
    colors = await db.fetch(
        "SELECT dominant_color AS value, COUNT(*) AS n FROM tracks "
        "WHERE dominant_color IS NOT NULL GROUP BY dominant_color ORDER BY n DESC"
    )
    event_types = await db.fetch(
        "SELECT event_type AS value, COUNT(*) AS n, "
        "COUNT(*) FILTER (WHERE status = 'new') AS open FROM events GROUP BY event_type ORDER BY n DESC"
    )
    span = await db.fetchrow("SELECT MIN(ts) AS from_ts, MAX(ts) AS to_ts FROM detections")
    return {"classes": classes, "colors": colors, "event_types": event_types, "coverage": span}
