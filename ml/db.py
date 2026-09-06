import json
import logging
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor
from pgvector.psycopg2 import register_vector

from ml.config import settings
from ml.pipeline import Detection

log = logging.getLogger(__name__)

def get_connection():
    conn = psycopg2.connect(settings.database_url, cursor_factory=RealDictCursor)
    register_vector(conn)
    return conn

def get_camera_id(conn, external_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM cameras WHERE external_id = %s", (external_id,))
        row = cur.fetchone()
        return str(row['id']) if row else None

def write_detection(conn, det: Detection, session_key: str = "live") -> None:
    """Persist one detection.

    ``session_key`` namespaces ``track_id``: ByteTrack numbers tracks from 1
    within a run, so a batch-index pass and the live worker will both emit
    track 7 for unrelated vehicles. Callers that jump through time (batch_index)
    should pass a distinct key per run so downstream track aggregation does not
    weld them together.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO detections 
            (id, camera_id, track_id, class, bbox, confidence, plate_text, plate_confidence, embedding, thumbnail_key, ts, pts_ms, dominant_color, frame_w, frame_h, session_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            det.id, det.camera_id, det.track_id, det.class_name, json.dumps(det.bbox), 
            det.confidence, det.plate_text, det.plate_confidence, 
            det.embedding, det.thumbnail_key, det.ts, det.pts_ms, det.dominant_color,
            getattr(det, "frame_w", None), getattr(det, "frame_h", None), session_key,
        ))

def write_alert(
    conn,
    watchlist_entry_id: str | None,
    detection_id: str,
    alert_type: str = "watchlist_match",
) -> None:
    """Write an alert row.

    Args:
        watchlist_entry_id: UUID of the matching watchlist entry, or None for
            rule-based anomaly alerts (stopped_vehicle, wrong_way, etc.).
        detection_id: UUID of the detection that triggered the alert.
        alert_type: One of 'watchlist_match', 'stopped_vehicle', 'wrong_way'.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type, status)
            VALUES (%s, %s, %s, 'pending')
        """, (watchlist_entry_id, detection_id, alert_type))

def commit(conn):
    try:
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"Failed to commit: {e}")
        raise
