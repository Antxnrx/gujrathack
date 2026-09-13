"""Backend settings.

Deliberately a separate Settings class from ``ml.config``. The two layers do not
share a deployment: ``ml/*`` runs on an inference box with a GPU and model
checkpoints, the API runs wherever it can reach Postgres. Reusing one settings
object would mean the API refusing to start because a YOLO weight path was
missing, which is how a service ends up with a mandatory dependency nobody
intended.

Where they *do* overlap — ``DATABASE_URL``, the MinIO block, ``SENTINEL_*`` — the
env var names are identical, so one ``.env`` drives both.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = "postgresql://setu:setu_dev_only@localhost:5432/setu"
    db_pool_min: int = 2
    db_pool_max: int = 12
    # Statement timeout for operator queries. A search that would take longer
    # than this is a search whose result nobody is still waiting for, and letting
    # it run holds a pool slot that the rest of the control room needs.
    db_statement_timeout_ms: int = 15_000

    # ── Model 1 (infrastructure service) ────────────────────────────────────
    # Its handover specifies RS256 JWTs verified against a JWKS endpoint, plus an
    # X-API-Key for service-to-service calls. Both are optional here: with
    # neither configured the API runs in `auth_optional` mode, which is what
    # makes local development and the offline demo possible. That mode is
    # refused when `require_auth` is on, so a deployment cannot fall into it by
    # forgetting to set a variable.
    model1_base_url: str = ""
    # Where an operator reaches Model 1's own portal, as opposed to its API. The
    # console links to it so the two halves feel like one product; it is a
    # separate setting because the API and the portal are separate deployments
    # and only coincidentally on the same host in development.
    model1_portal_url: str = ""
    model1_jwks_url: str = ""
    model1_api_key: str = ""
    model1_audience: str = "setu"
    model1_issuer: str = ""
    jwks_cache_seconds: int = 600
    require_auth: bool = False

    # ── Vehicle register (VAHAN) ────────────────────────────────────────────
    # Empty by default, and the lookup stays disabled until both are set.
    # Authorised VAHAN access is granted by MoRTH/NIC to an enforcement agency
    # on written application under the National Transport Repository
    # data-sharing policy — it is issued to the force, not obtained by the
    # application. See backend/app/services/vahan.py for why API Setu's public
    # RC endpoint cannot substitute.
    vahan_base_url: str = ""
    vahan_api_key: str = ""

    # ── Object storage (thumbnails, evidence) ───────────────────────────────
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "setu"
    minio_secret_key: str = "setu_dev_only"
    minio_bucket: str = "setu-thumbnails"
    minio_secure: bool = False
    presign_expiry_seconds: int = 900

    # ── Camera streams ──────────────────────────────────────────────────────
    sentinel_base_url: str = "https://cctv.corp8.cloud"
    sentinel_username: str = ""
    sentinel_password: str = ""
    # The gateway rejects requests without a browser UA and serves no CORS
    # headers, so the browser cannot fetch the playlist directly. See
    # routers/stream.py for why the relay is host-locked.
    stream_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    stream_relay_timeout_s: float = 20.0
    # Playlists get their own, much shorter budget. A live playlist describes a window
    # a few seconds wide, so a fetch that takes twenty seconds has not been slow — it
    # has answered a question about a moment that has already passed, and the player
    # gave up long before. Segments keep the 20 s.
    stream_playlist_timeout_s: float = 6.0

    # ── Search / ranking ────────────────────────────────────────────────────
    search_default_limit: int = 60
    search_max_limit: int = 300
    # CLIP measures 0.76–0.91 cosine between *completely different* vehicles
    # (see ml/watchlist.py). Any absolute threshold on that number is arbitrary,
    # so it is used for ordering and as a likelihood ratio against a per-camera
    # background — never as a yes/no identity test.
    # Read from CLIP_MODEL — the same variable ``ml.config`` uses — so one .env
    # line drives both halves. It used to read CLIP_MODEL_NAME, which meant
    # setting CLIP_MODEL moved the worker's checkpoint and left the API's text
    # encoder behind, putting queries and crops in different vector spaces. That
    # failure has no exception to raise: search just returns confident nonsense.
    # CLIP_MODEL_NAME is still accepted so existing deployments keep working.
    clip_model_name: str = Field(
        default="ViT-B-32-quickgelu",
        validation_alias=AliasChoices("CLIP_MODEL", "CLIP_MODEL_NAME"),
    )
    clip_pretrained: str = "openai"

    # ── Realtime ────────────────────────────────────────────────────────────
    sse_keepalive_seconds: float = 15.0
    sse_queue_size: int = 200

    # ── Camera transition graph ─────────────────────────────────────────────
    # The graph is what turns 30 independent cameras into a network you can follow
    # a vehicle across. Two kinds of edge: geography (always available, weak) and
    # observed plate transitions (strong, but only where plates were read).
    #
    # 25 km/h is the honest average for arterial Ahmedabad traffic including
    # signals, and the detour factor accounts for straight-line distance being a
    # lower bound on road distance — without it every ETA window opens too early
    # and closes before the vehicle arrives.
    graph_max_link_distance_m: float = 5000.0
    graph_urban_speed_kmh: float = 25.0
    graph_free_speed_kmh: float = 55.0
    graph_congested_speed_kmh: float = 10.0
    graph_detour_factor: float = 1.35
    # A gap longer than this is not a transition, it is two separate journeys. A
    # plate seen at 09:00 and again at 14:00 says nothing about a route.
    graph_transit_horizon_s: int = 1800
    graph_min_observations: int = 2
    # Small-sample shrinkage for observed transit windows, and it is load-bearing.
    # `percentile_cont(0.1)` over two sightings does not estimate a 10th percentile —
    # it returns a point 10% of the way between the two numbers it was given. Measured
    # on this dataset: 12 of 13 plate edges have n <= 3, and their p10–p90 windows ran
    # 0.5%–25% of the median (one was 1.3 s wide around a 260 s transit) against 205%
    # for a geographic edge over the same ground. The consequence is an inversion — the
    # strongest route evidence in the system produced the narrowest watch window, so the
    # reconstruction examined nothing at exactly the cameras it was most confident
    # about. The window is therefore blended toward a physically-derived band with
    # weight n / (n + this), which is ~0.2 at n=2 and ~0.7 at n=20: the observed median
    # is trusted immediately (observations genuinely measure it), the observed *spread*
    # only as it earns it.
    graph_transit_shrinkage: float = 8.0

    # ── Dragnet (cross-camera reconstruction) ───────────────────────────────
    dragnet_max_hops: int = 4
    dragnet_horizon_minutes: int = 120
    # Two different gates, because they answer different questions. The evidence
    # ratio is "how much more consistent with the subject than with chance" — it is
    # independent of how likely the subject was to be at that camera at all, and it
    # is what decides whether a candidate is worth an operator's attention. The
    # posterior additionally folds in the corridor prior and the size of the
    # candidate pool, so it is the right thing to *rank* by and a poor thing to
    # threshold on: a genuine match through a weak corridor at a busy camera has a
    # low posterior and is still the lead worth following.
    dragnet_min_evidence_ratio: float = 8.0
    dragnet_min_score: float = 0.02
    dragnet_max_candidates_per_camera: int = 500
    # Size of the same-class background sample used to calibrate a CLIP cosine
    # into a likelihood ratio. Too small and the null distribution is noise; too
    # large and every hop pays for it. 400 crops is ~2 ms of numpy.
    dragnet_background_sample: int = 400
    # Above a single genuine character difference on ml.analytics.anpr's scale;
    # see services/dragnet.py for the measured bands this sits between.
    dragnet_plate_similarity_floor: float = 0.92

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── Process ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    # Slow-request logging. Not a metric — a breadcrumb for the one query that made
    # an operator wait, with the path and the duration, so it can be found without
    # reproducing it.
    slow_request_ms: float = 1500.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> BackendSettings:
    return BackendSettings()


settings = get_settings()
