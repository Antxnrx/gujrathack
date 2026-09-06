"""Request bodies, and only request bodies.

Reads return plain dicts straight from the SQL. That is a deliberate asymmetry.
The queries in the routers project exactly the columns the UI needs, joined and
aggregated the way the indexes support; wrapping each one in a response model
would mean maintaining a second declaration of the same shape whose only function
is to be kept in sync with the first, and whose failure mode — a column silently
dropped from a response because nobody added it to the model — is the kind of bug
that is found by an operator, not by a test.

Writes are the opposite. Everything here crosses a trust boundary, ends up in a
table with CHECK constraints, and is worth rejecting with a 422 that names the
field rather than a 500 from Postgres.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# The vocabularies below mirror the CHECK constraints in scripts/schema.sql and
# migrations/002_backend.sql. Duplicated on purpose: the database is the authority
# and will reject anything wrong regardless, but a 422 naming the field is a much
# better answer than a 500 carrying a constraint name.
TargetType = Literal["event", "detection", "track", "camera", "incident", "pursuit", "alert"]
EventStatus = Literal["new", "acknowledged", "resolved", "false_positive"]
IncidentStatus = Literal["open", "investigating", "closed"]
Priority = Literal["low", "medium", "high", "critical"]
ZoneKind = Literal["restricted", "direction", "crowd", "abandon_watch", "count_line"]


class NoteCreate(BaseModel):
    target_type: TargetType
    target_id: str
    body: Annotated[str, Field(min_length=1, max_length=8000)]

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note body cannot be blank")
        return v


class NoteUpdate(BaseModel):
    body: Annotated[str, Field(min_length=1, max_length=8000)]


class BookmarkCreate(BaseModel):
    target_type: TargetType
    target_id: str
    label: Annotated[str, Field(max_length=200)] = ""


class TagCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#64748b"
    kind: Annotated[str, Field(max_length=32)] = "manual"


class TagApply(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=32)   # tag ids or names


class EventStatusUpdate(BaseModel):
    status: EventStatus
    note: Optional[Annotated[str, Field(max_length=2000)]] = None


class EventCreate(BaseModel):
    """An operator marking something the rules did not catch."""

    camera_id: str
    label: Annotated[str, Field(min_length=1, max_length=300)]
    started_at: Optional[datetime] = None
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    detection_id: Optional[str] = None
    track_uuid: Optional[str] = None
    pts_ms: Optional[float] = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class IncidentCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    description: Annotated[str, Field(max_length=8000)] = ""
    priority: Priority = "medium"
    event_ids: list[str] = Field(default_factory=list, max_length=200)


class IncidentUpdate(BaseModel):
    title: Optional[Annotated[str, Field(min_length=1, max_length=300)]] = None
    description: Optional[Annotated[str, Field(max_length=8000)]] = None
    status: Optional[IncidentStatus] = None
    priority: Optional[Priority] = None


class IncidentEventAdd(BaseModel):
    event_ids: list[str] = Field(min_length=1, max_length=200)
    note: Optional[Annotated[str, Field(max_length=2000)]] = None


class IncidentReorder(BaseModel):
    """The investigator's explicit ordering of a case file.

    A named model rather than a bare ``list[str]`` body for the same reason as every
    other write here: the bare list carried no length cap, so the request body alone
    decided how many UPDATE statements the handler ran, and its 422 said "Input should
    be a valid list" without naming the field that was wrong.
    """

    event_ids: list[str] = Field(min_length=1, max_length=200)


class WatchlistCreate(BaseModel):
    type: Literal["plate", "embedding"] = "plate"
    value: Optional[Annotated[str, Field(max_length=32)]] = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("value")
    @classmethod
    def _normalise_plate(cls, v: Optional[str]) -> Optional[str]:
        # ml/plate_validator.py normalises to upper-case alphanumerics; a watchlist
        # entry stored as "GJ 01 AB 1234" would never match a read of "GJ01AB1234".
        if v is None:
            return None
        return "".join(ch for ch in v.upper() if ch.isalnum())


class ZoneCreate(BaseModel):
    camera_id: str
    name: Annotated[str, Field(min_length=1, max_length=120)]
    kind: ZoneKind
    polygon: list[list[float]] = Field(min_length=3, max_length=64)
    direction_deg: Optional[float] = None
    # These two mirror the DDL's own defaults (`tolerance_deg REAL NOT NULL DEFAULT 75`,
    # `classes TEXT[] NOT NULL DEFAULT '{}'`) the same way `params` and `active` below
    # already do — and not as `Optional[... ] = None`, which is what they were. An
    # explicitly bound NULL does not fall back to a column default, it violates the
    # constraint, so every zone POST 500'd and none of the seven detector rules could be
    # configured through the API at all.
    #
    # 180 is the cap because tolerance is measured off a reference heading in both
    # directions: at 180 every heading is inside the permitted arc, so anything larger
    # is not a loose zone, it is a wrong-way rule silently switched off.
    tolerance_deg: Annotated[float, Field(ge=0, le=180)] = 75.0
    # Empty means all classes, which is what the DDL's '{}' means too. There is no third
    # state for this to express, so there is no reason for it to be nullable.
    classes: list[str] = Field(default_factory=list, max_length=30)
    params: dict[str, Any] = Field(default_factory=dict)
    active: bool = True

    @field_validator("polygon")
    @classmethod
    def _normalised(cls, poly: list[list[float]]) -> list[list[float]]:
        # Zones are stored normalised so they survive a resolution change on the
        # source feed. A polygon in pixels would silently sit in the top-left
        # corner of the frame and match nothing.
        for point in poly:
            if len(point) != 2:
                raise ValueError("each polygon point must be [x, y]")
            if not all(0.0 <= c <= 1.0 for c in point):
                raise ValueError("polygon coordinates must be normalised to 0–1")
        return poly


class EvidenceCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    incident_id: Optional[str] = None
    event_ids: list[str] = Field(default_factory=list, max_length=500)
    detection_ids: list[str] = Field(default_factory=list, max_length=500)
    track_ids: list[str] = Field(default_factory=list, max_length=500)
    pursuit_id: Optional[str] = None
    note: Annotated[str, Field(max_length=4000)] = ""


class SearchRequest(BaseModel):
    """Feature 4 — one endpoint for time, location, camera, plate and text."""

    q: Optional[Annotated[str, Field(max_length=500)]] = None
    plate: Optional[Annotated[str, Field(max_length=32)]] = None
    camera_ids: list[str] = Field(default_factory=list, max_length=100)
    districts: list[str] = Field(default_factory=list, max_length=50)
    classes: list[str] = Field(default_factory=list, max_length=30)
    colors: list[str] = Field(default_factory=list, max_length=20)
    from_ts: Optional[datetime] = None
    to_ts: Optional[datetime] = None
    near: Optional[tuple[float, float]] = None       # (lat, lng)
    radius_km: Optional[Annotated[float, Field(gt=0, le=500)]] = None
    min_plate_confidence: Optional[Annotated[float, Field(ge=0, le=1)]] = None
    level: Literal["track", "detection"] = "track"
    limit: Annotated[int, Field(ge=1, le=300)] = 60
    offset: Annotated[int, Field(ge=0, le=10000)] = 0

    @field_validator("plate")
    @classmethod
    def _normalise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return "".join(ch for ch in v.upper() if ch.isalnum()) or None


class DragnetStart(BaseModel):
    """Seed a pursuit from something an operator is already looking at."""

    title: Optional[Annotated[str, Field(max_length=300)]] = None
    track_uuid: Optional[str] = None
    detection_id: Optional[str] = None
    plate_text: Optional[Annotated[str, Field(max_length=32)]] = None
    incident_id: Optional[str] = None
    horizon_minutes: Annotated[int, Field(ge=1, le=720)] = 60
    max_hops: Annotated[int, Field(ge=1, le=6)] = 3
    # Two gates on different scales — see services/dragnet.py. The evidence ratio is
    # "how many times more consistent with the subject than with chance" and is what
    # decides whether a candidate is worth showing; min_score thresholds the posterior,
    # which already accounts for how unlikely the subject was to be at that camera and
    # so runs low even for good leads.
    min_evidence_ratio: Annotated[float, Field(ge=1, le=10_000)] = 8.0
    min_score: Annotated[float, Field(ge=0, le=1)] = 0.02
    persist: bool = True

    @field_validator("plate_text")
    @classmethod
    def _normalise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return "".join(ch for ch in v.upper() if ch.isalnum()) or None


class SightingReview(BaseModel):
    confirmed: bool


class NextHop(BaseModel):
    """One hop of a manual camera-to-camera walk (feature 9).

    The same scoring engine as a full reconstruction, aimed at a single camera the
    operator chose. Separate from DragnetStart because this one asserts nothing and
    saves nothing — it answers "is my vehicle in this camera's next two minutes".
    """

    track_uuid: Optional[str] = None
    detection_id: Optional[str] = None
    to_camera: Optional[str] = None
    max_cameras: Annotated[int, Field(ge=1, le=12)] = 6
    horizon_minutes: Annotated[int, Field(ge=1, le=240)] = 30
    min_evidence_ratio: Annotated[float, Field(ge=0, le=10_000)] = 3.0
    limit_per_camera: Annotated[int, Field(ge=1, le=50)] = 8


class CameraLinkUpsert(BaseModel):
    src_camera_id: str
    dst_camera_id: str
    median_transit_s: Annotated[float, Field(gt=0, le=86400)]
    distance_m: Optional[Annotated[float, Field(ge=0)]] = None
