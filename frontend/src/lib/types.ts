/**
 * Shapes as the API actually returns them, transcribed from live responses rather
 * than from the router source, because the reads are deliberately un-modelled on the
 * server (see backend/app/schemas.py) and the SQL projection is the only authority.
 *
 * Every field the server may leave NULL is `| null` here, not optional. A camera with
 * no detections yet returns `first_ts: null`, and code that treats that as "not loaded"
 * renders a spinner forever.
 */

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
  count: number;
  has_more?: boolean;
  total?: number;
}

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type EventStatus = "new" | "acknowledged" | "resolved" | "false_positive";

export interface Tag { id: string; name: string; color: string; kind: string; usage?: number }

/** What the backend attaches alongside every plate it returns.
 *
 * `plate_trust` is how far the read can be trusted; `plate_origin` is the
 * registering authority the plate itself names ("GJ 11 · Junagadh"); and
 * `rto_known` is false for a code Gujarat has never issued, which is a reason
 * to distrust a read even when every character looks crisp. */
export interface PlateMeta {
  plate_trust?: { tier: string; label: string; guidance: string } | null;
  plate_district?: string | null;
  plate_origin?: string | null;
  rto_known?: boolean;
}

export interface EventRow extends PlateMeta {
  id: string;
  camera_id: string;
  event_type: string;
  severity: Severity;
  status: EventStatus;
  label: string;
  started_at: string;
  ended_at: string | null;
  pts_ms: number | null;
  confidence: number | null;
  metrics: Record<string, unknown>;
  thumbnail_key: string | null;
  thumbnail_url: string | null;
  plate_text: string | null;
  track_uuid: string | null;
  detection_id: string | null;
  alert_id: string | null;
  zone_id: string | null;
  zone_name: string | null;
  source: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  camera_external_id: string;
  camera_name: string;
  district: string | null;
  area: string | null;
  lat: number | null;
  lng: number | null;
  tags: Tag[];
  incident_id: string | null;
  incident_ref: string | null;
  note_count: number;
  bookmarked: boolean;
}

export interface Camera {
  id: string;
  external_id: string;
  name: string;
  district: string | null;
  area: string | null;
  lat: number | null;
  lng: number | null;
  status: string;
  geocode_precision: string | null;
  detection_count: number;
  plate_count: number;
  track_count: number;
  event_count: number;
  open_event_count: number;
  zone_count: number;
  first_ts: string | null;
  last_ts: string | null;
  class_mix: Record<string, number>;
  color_mix: Record<string, number>;
  has_stream: boolean;
  department: string | null;
  camera_uid: string | null;
  metadata: Record<string, unknown> & { source_system?: string; protocol?: string };
}

export interface SourceSystem {
  source_system: string;
  department: string | null;
  cameras: number;
  active: number;
  districts: number;
  protocols: string[];
  example_uid: string | null;
}

export interface TrackRow extends PlateMeta {
  id: string;
  camera_id: string;
  track_id: number;
  class: string | null;
  dominant_color: string | null;
  plate_text: string | null;
  plate_confidence: number | null;
  plate_read_count: number;
  first_seen: string;
  last_seen: string;
  first_pts_ms: number | null;
  last_pts_ms: number | null;
  duration_s: number | null;
  detection_count: number;
  thumbnail_key: string | null;
  thumbnail_url: string | null;
  heading_deg: number | null;
  displacement_norm: number | null;
  max_spread_norm: number | null;
  best_detection_id: string | null;
  source: string | null;
  camera_external_id: string;
  camera_name: string;
  district: string | null;
}

export interface AlertRow extends PlateMeta {
  id: string;
  alert_type: string;
  status: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  watchlist_entry_id: string | null;
  watchlist_value: string | null;
  watchlist_reason: string | null;
  watchlist_type: string | null;
  detection_id: string | null;
  plate_text: string | null;
  plate_confidence: number | null;
  class: string | null;
  dominant_color: string | null;
  detection_confidence: number | null;
  ts: string;
  thumbnail_url: string | null;
  track_uuid: string | null;
  camera_external_id: string;
  camera_name: string;
  district: string | null;
  lat: number | null;
  lng: number | null;
  event_id: string | null;
  consensus_plate: string | null;
  consensus_confidence: number | null;
  plate_read_count: number | null;
}

export interface Note {
  id: string;
  body: string;
  operator: string;
  created_at: string;
  updated_at?: string | null;
  target_type: string;
  target_id?: string;
}

export interface Incident {
  id: string;
  ref: string;
  title: string;
  description: string;
  status: "open" | "investigating" | "closed";
  priority: "low" | "medium" | "high" | "critical";
  created_at: string;
  updated_at: string | null;
  created_by: string | null;
  event_count?: number;
  camera_count?: number;
  first_event_at?: string | null;
  last_event_at?: string | null;
}

export interface Zone {
  id: string;
  camera_id: string;
  name: string;
  kind: string;
  polygon: number[][];
  direction_deg: number | null;
  tolerance_deg: number;
  classes: string[];
  params: Record<string, unknown>;
  active: boolean;
  camera_external_id?: string;
  camera_name?: string;
}

export interface Facets {
  classes: { value: string; n: number }[];
  colors: { value: string; n: number }[];
  event_types: { value: string; n: number; open: number }[];
  districts?: { value: string; n: number }[];
  coverage: { from_ts: string | null; to_ts: string | null };
}
