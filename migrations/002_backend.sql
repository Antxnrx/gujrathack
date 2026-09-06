-- ─────────────────────────────────────────────────────────────────────────────
-- Setu — Migration 002: Backend / operator layer
-- ─────────────────────────────────────────────────────────────────────────────
-- Additive over scripts/schema.sql (the ML-owned contract). Nothing here drops
-- or renames an ML column; ml/db.py keeps working unchanged.
--
-- Apply:  psql $DATABASE_URL -f migrations/002_backend.sql
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy plate search

-- ═══════════════════════════════════════════════════════════════════════════
-- 0. Repair drift in ML-owned tables
-- ═══════════════════════════════════════════════════════════════════════════

-- The deployed `alerts` table predates alert_type and made watchlist_entry_id
-- NOT NULL, which makes rule-based anomaly alerts (stopped_vehicle, wrong_way…)
-- impossible to insert. schema.sql already says otherwise; reconcile the DB.
ALTER TABLE alerts ALTER COLUMN watchlist_entry_id DROP NOT NULL;

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS alert_type TEXT NOT NULL
      DEFAULT 'watchlist_match';

-- Widen the alert_type vocabulary to cover every rule the analytics engine emits.
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_alert_type_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_alert_type_check CHECK (
    alert_type IN (
        'watchlist_match', 'stopped_vehicle', 'wrong_way', 'loitering',
        'crowd', 'zone_intrusion', 'abandoned_object', 'accident_candidate',
        'anpr_hit', 'pursuit_sighting', 'manual'
    )
);

-- Distinguish synthetic demo rows from real inference output, permanently.
-- DEFAULT means ml/db.py's INSERT (which does not name this column) still works.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live';

-- Batch runs jump through time, so a track_id is only unique within one run.
-- session_key namespaces it. NULL = the legacy/live session.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS session_key TEXT;

CREATE INDEX IF NOT EXISTS ix_detections_ts        ON detections (ts DESC);
CREATE INDEX IF NOT EXISTS ix_detections_class     ON detections (class);
CREATE INDEX IF NOT EXISTS ix_detections_color     ON detections (dominant_color);
CREATE INDEX IF NOT EXISTS ix_detections_track     ON detections (camera_id, track_id, ts);
CREATE INDEX IF NOT EXISTS ix_detections_plate_trgm
    ON detections USING gin (plate_text gin_trgm_ops) WHERE plate_text IS NOT NULL;

-- Camera enrichment for search-by-location. Model 1 owns the registry; these
-- mirror its fields so the backend can filter without a round trip.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS camera_uid       TEXT;   -- GJ-POL-000123, Model 1 join key
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS district         TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS area             TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS department       TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS geocode_precision TEXT DEFAULT 'district';
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS metadata         JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS ix_cameras_district ON cameras (district);

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. tracks — track-level identity (feature 11, 12)
-- ═══════════════════════════════════════════════════════════════════════════
-- One row per (camera, track_id, session). This is where multi-frame ANPR
-- consensus lands: per-character confidence-weighted voting across every frame
-- the track was seen in, which is what makes blurry plates readable at all.
CREATE TABLE IF NOT EXISTS tracks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id         UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    track_id          INTEGER NOT NULL,
    session_key       TEXT NOT NULL DEFAULT 'live',
    class             TEXT NOT NULL,
    class_votes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    dominant_color    TEXT,
    color_votes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    plate_text        TEXT,                    -- multi-frame consensus, not a single read
    plate_confidence  REAL,
    plate_read_count  INTEGER NOT NULL DEFAULT 0,
    plate_votes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen        TIMESTAMPTZ NOT NULL,
    last_seen         TIMESTAMPTZ NOT NULL,
    first_pts_ms      DOUBLE PRECISION,
    last_pts_ms       DOUBLE PRECISION,
    duration_s        REAL NOT NULL DEFAULT 0,
    detection_count   INTEGER NOT NULL DEFAULT 0,
    best_detection_id UUID REFERENCES detections(id) ON DELETE SET NULL,
    thumbnail_key     TEXT,
    path              JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{t,x,y,w,h}] normalised centroids
    heading_deg       REAL,                                  -- net travel direction, image space
    displacement_norm REAL,                                  -- net centroid travel / frame diagonal
    max_spread_norm   REAL,                                  -- bounding radius of the path
    embedding         vector(512),                           -- mean of member embeddings, renormalised
    source            TEXT NOT NULL DEFAULT 'live',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (camera_id, track_id, session_key)
);
CREATE INDEX IF NOT EXISTS ix_tracks_camera_time ON tracks (camera_id, first_seen DESC);
CREATE INDEX IF NOT EXISTS ix_tracks_plate       ON tracks (plate_text) WHERE plate_text IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tracks_plate_trgm
    ON tracks USING gin (plate_text gin_trgm_ops) WHERE plate_text IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tracks_embedding
    ON tracks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_tracks_class_color ON tracks (class, dominant_color);

-- Which detections belong to which track (kept explicit — track_id alone is
-- ambiguous once you have multiple sessions).
ALTER TABLE detections ADD COLUMN IF NOT EXISTS track_uuid UUID REFERENCES tracks(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_detections_track_uuid ON detections (track_uuid);

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. zones — restricted areas & direction references (features 16, 19, 20)
-- ═══════════════════════════════════════════════════════════════════════════
-- Polygons are normalised [0,1] image coordinates so they survive a resolution
-- change on the source feed.
CREATE TABLE IF NOT EXISTS zones (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id     UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('restricted','direction','crowd','abandon_watch','count_line')),
    polygon       JSONB NOT NULL,          -- [[x,y], …] normalised 0–1
    direction_deg REAL,                    -- permitted heading, for kind='direction'
    tolerance_deg REAL NOT NULL DEFAULT 75,
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    classes       TEXT[] NOT NULL DEFAULT '{}',   -- empty = all classes
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_zones_camera ON zones (camera_id) WHERE active;

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. events — the operator-facing occurrence table (features 1, 15–20)
-- ═══════════════════════════════════════════════════════════════════════════
-- `alerts` stays exactly as the ML layer wrote it (low-latency notify channel).
-- `events` is the richer object an operator triages, tags, bookmarks and files
-- into an incident. Watchlist/stopped-vehicle alerts are mirrored in by the
-- backend's alert ingestor so there is one timeline, not two.
CREATE TABLE IF NOT EXISTS events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id      UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    event_type     TEXT NOT NULL CHECK (event_type IN (
                       'loitering','wrong_way','crowd','zone_intrusion',
                       'abandoned_object','accident_candidate','stopped_vehicle',
                       'watchlist_match','anpr_hit','pursuit_sighting','manual')),
    severity       TEXT NOT NULL DEFAULT 'medium'
                        CHECK (severity IN ('info','low','medium','high','critical')),
    status         TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','acknowledged','resolved','false_positive')),
    label          TEXT NOT NULL DEFAULT '',
    track_uuid     UUID REFERENCES tracks(id) ON DELETE SET NULL,
    detection_id   UUID REFERENCES detections(id) ON DELETE SET NULL,
    alert_id       UUID REFERENCES alerts(id) ON DELETE SET NULL,
    zone_id        UUID REFERENCES zones(id) ON DELETE SET NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ,
    pts_ms         DOUBLE PRECISION,
    confidence     REAL NOT NULL DEFAULT 0.5,
    metrics        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- dwell_s, person_count, angle_delta…
    thumbnail_key  TEXT,
    plate_text     TEXT,
    reviewed_by    TEXT,
    reviewed_at    TIMESTAMPTZ,
    dedupe_key     TEXT UNIQUE,     -- rule idempotency: re-running the engine won't duplicate
    source         TEXT NOT NULL DEFAULT 'analytics',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_events_started    ON events (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_camera     ON events (camera_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_type       ON events (event_type, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_status     ON events (status) WHERE status = 'new';
CREATE INDEX IF NOT EXISTS ix_events_alert      ON events (alert_id);

-- Live push for the frontend: one channel for operator-facing events.
CREATE OR REPLACE FUNCTION _notify_setu_event()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('setu_events', json_build_object(
        'id', NEW.id, 'camera_id', NEW.camera_id, 'event_type', NEW.event_type,
        'severity', NEW.severity, 'started_at', NEW.started_at
    )::text);
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tr_event_notify ON events;
CREATE TRIGGER tr_event_notify AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION _notify_setu_event();

-- Mirror alerts onto the event timeline.
--
-- `ml/db.py:write_alert` is the only writer of `alerts`, and it knows nothing
-- about events — it predates them. But an operator has one timeline, and "this
-- vehicle is on the watchlist" belongs on it next to "this vehicle stopped in a
-- live lane". The `events.alert_id` column exists for exactly this join.
--
-- Done as a trigger rather than as an ingestor in the API on purpose. An
-- application-level mirror can fall behind, and anything it misses while the
-- process is restarting is a watchlist hit that never reached the operator. In
-- the database the mirror runs in the same transaction as the alert: either both
-- rows exist or neither does. It also means the existing tr_event_notify pushes
-- the mirrored row live, with no second channel to keep in sync.
--
-- Only alert types with no other producer are mirrored. The seven rule types
-- (loitering, wrong_way, crowd, zone_intrusion, abandoned_object,
-- accident_candidate, stopped_vehicle) are written directly as events by
-- `ml/analytics/rules.py`, with the metrics, zone link and stable dedupe key an
-- operator actually investigates. Mirroring those as well would put two rows on
-- the timeline for one real-world event — and the poorer of the two would be the
-- one that got there first. `ml/worker.py`'s live stopped_vehicle alert stays
-- visible through /api/alerts until the analytics pass promotes it.
CREATE OR REPLACE FUNCTION _mirror_alert_to_event()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    d          RECORD;
    wl_value   TEXT;
    ev_label   TEXT;
    ev_sev     TEXT;
BEGIN
    IF NEW.alert_type NOT IN ('watchlist_match', 'anpr_hit', 'pursuit_sighting', 'manual') THEN
        RETURN NEW;
    END IF;

    SELECT camera_id, ts, pts_ms, thumbnail_key, plate_text, plate_confidence,
           class, track_uuid
      INTO d
      FROM detections WHERE id = NEW.detection_id;
    IF NOT FOUND THEN
        RETURN NEW;          -- alert without a detection: nothing to place on a timeline
    END IF;

    SELECT value INTO wl_value FROM watchlist_entries WHERE id = NEW.watchlist_entry_id;

    ev_sev   := CASE WHEN NEW.alert_type = 'watchlist_match' THEN 'critical' ELSE 'high' END;
    ev_label := CASE NEW.alert_type
                    WHEN 'watchlist_match' THEN
                        'Watchlist match' || COALESCE(': ' || wl_value, '')
                    ELSE
                        initcap(replace(NEW.alert_type, '_', ' '))
                        || COALESCE(' — ' || d.plate_text, COALESCE(' — ' || d.class, ''))
                END;

    INSERT INTO events (camera_id, event_type, severity, status, label, alert_id,
                        detection_id, track_uuid, started_at, pts_ms, confidence,
                        thumbnail_key, plate_text, metrics, dedupe_key, source)
    VALUES (d.camera_id, NEW.alert_type, ev_sev, 'new', ev_label, NEW.id,
            NEW.detection_id, d.track_uuid, d.ts, d.pts_ms,
            -- The confidence an operator cares about here is "is the plate read
            -- right", not "is there a car" — a watchlist match is an exact string
            -- comparison, so the read is the only uncertain step.
            COALESCE(d.plate_confidence, 0.5),
            d.thumbnail_key, d.plate_text,
            jsonb_build_object('watchlist_entry_id', NEW.watchlist_entry_id,
                               'watchlist_value', wl_value,
                               'mirrored_from', 'alerts'),
            'alert:' || NEW.id::text, 'alerts')
    ON CONFLICT (dedupe_key) DO NOTHING;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tr_alert_mirror ON alerts;
CREATE TRIGGER tr_alert_mirror AFTER INSERT ON alerts
    FOR EACH ROW EXECUTE FUNCTION _mirror_alert_to_event();

-- ═══════════════════════════════════════════════════════════════════════════
-- 4. tags — vocabulary as data, not an enum (feature 1)
-- ═══════════════════════════════════════════════════════════════════════════
-- Model 1's handover is explicit: "Vocabulary is data, not enums. If you
-- hardcode an enum, the first department that sends something new breaks your
-- service and not ours." Applied here.
CREATE TABLE IF NOT EXISTS tags (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT UNIQUE NOT NULL,
    color      TEXT NOT NULL DEFAULT '#64748b',
    kind       TEXT NOT NULL DEFAULT 'general',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS event_tags (
    event_id   UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    tag_id     UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_event_tags_tag ON event_tags (tag_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- 5. incidents — case files and their timelines (feature 3)
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS incidents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ref         TEXT UNIQUE,        -- human handle: INC-2026-0001
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','investigating','closed')),
    priority    TEXT NOT NULL DEFAULT 'medium'
                     CHECK (priority IN ('low','medium','high','critical')),
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at   TIMESTAMPTZ
);
CREATE SEQUENCE IF NOT EXISTS incident_ref_seq START 1;

CREATE TABLE IF NOT EXISTS incident_events (
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_id    UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    sequence_no INTEGER,
    note        TEXT NOT NULL DEFAULT '',
    added_by    TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (incident_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_incident_events_event ON incident_events (event_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- 6. bookmarks + notes — polymorphic, one table each (features 6, 7)
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS bookmarks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type TEXT NOT NULL CHECK (target_type IN ('detection','event','incident','camera','track','pursuit')),
    target_id   UUID NOT NULL,
    operator    TEXT NOT NULL DEFAULT 'operator',
    label       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target_type, target_id, operator)
);
CREATE INDEX IF NOT EXISTS ix_bookmarks_operator ON bookmarks (operator, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type TEXT NOT NULL CHECK (target_type IN ('detection','event','incident','camera','track','pursuit')),
    target_id   UUID NOT NULL,
    body        TEXT NOT NULL,
    operator    TEXT NOT NULL DEFAULT 'operator',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_notes_target ON notes (target_type, target_id, created_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════
-- 7. evidence snapshots (feature 5)
-- ═══════════════════════════════════════════════════════════════════════════
-- An immutable manifest of what an operator captured, with a SHA-256 over the
-- manifest so an export can be shown not to have been edited after the fact.
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ref         TEXT UNIQUE,
    incident_id UUID REFERENCES incidents(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    operator    TEXT NOT NULL DEFAULT 'operator',
    manifest    JSONB NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE SEQUENCE IF NOT EXISTS evidence_ref_seq START 1;
CREATE INDEX IF NOT EXISTS ix_evidence_incident ON evidence_snapshots (incident_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- 8. audit_log — who looked at what
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    operator   TEXT NOT NULL DEFAULT 'operator',
    action     TEXT NOT NULL,
    target     TEXT,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log (created_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════
-- 9. DRAGNET — cross-camera pursuit (features 9 + the predictive layer)
-- ═══════════════════════════════════════════════════════════════════════════
-- Design note, because it matters and it is easy to get wrong:
--
-- ml/watchlist.py disabled embedding-based identity matching for a measured
-- reason — inter-vehicle CLIP cosine is 0.76–0.91 for *completely different*
-- vehicles, so no absolute threshold separates "same vehicle" from "different
-- vehicle of the same type". Dragnet does not repeat that mistake. It never
-- uses raw cosine as an identity oracle. Instead:
--
--   1. camera_links gives a spatio-temporal corridor: from camera A at time T,
--      only cameras reachable inside a learned transit window are candidates.
--      That collapses the search space from millions of rows to dozens.
--   2. class + colour act as categorical filters inside that window.
--   3. a plate (from multi-frame consensus in `tracks`) is the identity anchor
--      when one exists — exact, then confusion-aware fuzzy.
--   4. CLIP only *ranks the survivors*, and is scored as a likelihood ratio
--      against the same-class background at that camera, not as an absolute
--      threshold. 0.88 means nothing alone; 0.88 where the background is
--      0.79 ± 0.02 is +4.5σ and means a great deal.
--   5. nothing is asserted. Every sighting is a scored candidate an operator
--      confirms or rejects, and confirmations feed camera_links back.

-- The camera transition graph, learned from confirmed co-observations rather
-- than from a hand-drawn road map.
CREATE TABLE IF NOT EXISTS camera_links (
    src_camera_id    UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    dst_camera_id    UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    observations     INTEGER NOT NULL DEFAULT 0,
    median_transit_s REAL,
    p10_transit_s    REAL,
    p90_transit_s    REAL,
    distance_m       REAL,
    probability      REAL NOT NULL DEFAULT 0,   -- P(dst next | departed src)
    evidence         TEXT NOT NULL DEFAULT 'geo',  -- geo | plate | confirmed
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (src_camera_id, dst_camera_id)
);
CREATE INDEX IF NOT EXISTS ix_camera_links_src ON camera_links (src_camera_id, probability DESC);

CREATE TABLE IF NOT EXISTS pursuits (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ref               TEXT UNIQUE,
    title             TEXT NOT NULL,
    subject_kind      TEXT NOT NULL DEFAULT 'vehicle'
                           CHECK (subject_kind IN ('vehicle','person','object')),
    subject_class     TEXT,
    subject_color     TEXT,
    plate_text        TEXT,
    seed_detection_id UUID REFERENCES detections(id) ON DELETE SET NULL,
    seed_track_uuid   UUID REFERENCES tracks(id) ON DELETE SET NULL,
    seed_camera_id    UUID REFERENCES cameras(id) ON DELETE SET NULL,
    seed_ts           TIMESTAMPTZ,
    embedding         vector(512),
    status            TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','paused','closed')),
    incident_id       UUID REFERENCES incidents(id) ON DELETE SET NULL,
    params            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at         TIMESTAMPTZ
);
CREATE SEQUENCE IF NOT EXISTS pursuit_ref_seq START 1;
CREATE INDEX IF NOT EXISTS ix_pursuits_status ON pursuits (status, created_at DESC);

-- Every candidate the reconstruction found, with *why* it scored what it did.
-- score_breakdown is deliberately exposed to the UI: an operator must be able
-- to see that a match rests on a plate rather than on a colour coincidence.
CREATE TABLE IF NOT EXISTS pursuit_sightings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pursuit_id      UUID NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
    detection_id    UUID REFERENCES detections(id) ON DELETE CASCADE,
    track_uuid      UUID REFERENCES tracks(id) ON DELETE SET NULL,
    camera_id       UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    pts_ms          DOUBLE PRECISION,
    score           REAL NOT NULL,
    match_kind      TEXT NOT NULL CHECK (match_kind IN ('seed','plate_exact','plate_fuzzy','visual','manual')),
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    thumbnail_key   TEXT,
    confirmed       BOOLEAN,        -- NULL = unreviewed, TRUE/FALSE = operator verdict
    reviewed_by     TEXT,
    sequence_no     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pursuit_id, detection_id)
);
CREATE INDEX IF NOT EXISTS ix_sightings_pursuit ON pursuit_sightings (pursuit_id, ts);

-- Where the subject is expected next, and when. Resolved when a sighting lands.
CREATE TABLE IF NOT EXISTS pursuit_predictions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pursuit_id   UUID NOT NULL REFERENCES pursuits(id) ON DELETE CASCADE,
    camera_id    UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    from_camera_id UUID REFERENCES cameras(id) ON DELETE SET NULL,
    eta_from     TIMESTAMPTZ NOT NULL,
    eta_to       TIMESTAMPTZ NOT NULL,
    probability  REAL NOT NULL,
    hops         INTEGER NOT NULL DEFAULT 1,
    via          JSONB NOT NULL DEFAULT '[]'::jsonb,
    resolved_by  UUID REFERENCES pursuit_sightings(id) ON DELETE SET NULL,
    expired      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pursuit_id, camera_id, hops)
);
CREATE INDEX IF NOT EXISTS ix_predictions_pursuit ON pursuit_predictions (pursuit_id, probability DESC);
CREATE INDEX IF NOT EXISTS ix_predictions_window  ON pursuit_predictions (eta_from, eta_to) WHERE NOT expired;

-- Live push for pursuit activity (a prediction confirmed is the moment that matters).
CREATE OR REPLACE FUNCTION _notify_setu_pursuit()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('setu_pursuits', json_build_object(
        'pursuit_id', NEW.pursuit_id, 'sighting_id', NEW.id,
        'camera_id', NEW.camera_id, 'ts', NEW.ts,
        'score', NEW.score, 'match_kind', NEW.match_kind
    )::text);
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tr_pursuit_notify ON pursuit_sightings;
CREATE TRIGGER tr_pursuit_notify AFTER INSERT ON pursuit_sightings
    FOR EACH ROW EXECUTE FUNCTION _notify_setu_pursuit();

-- ═══════════════════════════════════════════════════════════════════════════
-- 10. Camera-wise index (feature 2) — a materialised rollup, refreshed cheaply
-- ═══════════════════════════════════════════════════════════════════════════
-- Aggregate per (camera, class) and per (camera, colour) *first*, then fold each
-- into one jsonb object. The earlier form computed `n` with a window function
-- over raw detections and let `jsonb_object_agg` collapse the duplicates — which
-- gives the right answer (last value wins per key) but feeds the aggregate one
-- row per detection instead of one per class. At 52k detections that is 52k rows
-- through a jsonb builder to produce eight keys per camera, and it is the query
-- behind the camera list, so it is on the operator's critical path.
--
-- `color_mix` was declared and then hardcoded NULL. `detections.dominant_color`
-- exists now, so it is filled in rather than left as a column that always
-- returns nothing.
DROP VIEW IF EXISTS camera_index;
CREATE VIEW camera_index AS
WITH det AS (
    SELECT camera_id,
           COUNT(*)              AS detections,
           COUNT(plate_text)     AS plates,
           MIN(ts)               AS first_ts,
           MAX(ts)               AS last_ts
    FROM detections GROUP BY camera_id
),
class_mix AS (
    SELECT camera_id, jsonb_object_agg(class, n) AS mix FROM (
        SELECT camera_id, class, COUNT(*) AS n
        FROM detections WHERE class IS NOT NULL
        GROUP BY camera_id, class
    ) s GROUP BY camera_id
),
color_mix AS (
    SELECT camera_id, jsonb_object_agg(dominant_color, n) AS mix FROM (
        SELECT camera_id, dominant_color, COUNT(*) AS n
        FROM detections WHERE dominant_color IS NOT NULL
        GROUP BY camera_id, dominant_color
    ) s GROUP BY camera_id
)
SELECT
    c.id,
    c.external_id,
    c.name,
    c.district,
    c.area,
    c.lat,
    c.lng,
    c.status,
    c.geocode_precision,
    COALESCE(d.detections, 0)          AS detection_count,
    COALESCE(d.plates, 0)              AS plate_count,
    COALESCE(t.tracks, 0)              AS track_count,
    COALESCE(e.events, 0)              AS event_count,
    COALESCE(e.open_events, 0)         AS open_event_count,
    COALESCE(z.zones, 0)               AS zone_count,
    d.first_ts,
    d.last_ts,
    cm.mix                             AS class_mix,
    col.mix                            AS color_mix
FROM cameras c
LEFT JOIN det d        ON d.camera_id   = c.id
LEFT JOIN class_mix cm ON cm.camera_id  = c.id
LEFT JOIN color_mix col ON col.camera_id = c.id
LEFT JOIN (SELECT camera_id, COUNT(*) AS tracks FROM tracks GROUP BY camera_id) t ON t.camera_id = c.id
LEFT JOIN (SELECT camera_id, COUNT(*) AS events,
                  COUNT(*) FILTER (WHERE status = 'new') AS open_events
           FROM events GROUP BY camera_id) e ON e.camera_id = c.id
LEFT JOIN (SELECT camera_id, COUNT(*) AS zones FROM zones WHERE active GROUP BY camera_id) z ON z.camera_id = c.id;

-- ═══════════════════════════════════════════════════════════════════════════
-- 11. Frame dimensions on detections
-- ═══════════════════════════════════════════════════════════════════════════
-- Zone polygons are stored normalised (0–1) so they survive a resolution change
-- on the source feed, but `bbox` is in pixels. Testing one against the other
-- needs the frame size that produced the box. Nullable + defaulted, so
-- ml/db.py's INSERT keeps working for any row written before this column
-- existed; ml/analytics falls back to per-camera inference for those.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_w INTEGER;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_h INTEGER;
