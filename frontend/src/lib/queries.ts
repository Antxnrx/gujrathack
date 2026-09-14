import {
  useMutation, useQuery, useQueryClient, keepPreviousData, type QueryClient,
} from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "./api";
import { subscribe, EVENTS, ALERTS, PURSUITS } from "./live";
import type { AlertRow, Camera, DetectionRow, EventRow, Facets, Incident, Note, Page, PlaybookSpec, RegistrationCheck, SourceSystem, Tag, TrackRow, Zone } from "./types";

/**
 * Server state lives here, not in components.
 *
 * The latency requirement is mostly won in this file. Three decisions do the work:
 *
 * 1. `staleTime` is generous for things that do not change (the camera estate, the
 *    detector thresholds, search facets) and short for the queues an operator triages.
 *    A console that refetches 30 cameras on every navigation feels slow for no reason.
 * 2. `keepPreviousData` on every paginated list, so paging and filter changes repaint
 *    with the old rows dimmed instead of collapsing to a spinner and reflowing.
 * 3. Live updates *invalidate* rather than append. The SSE payload is a notification,
 *    not a row — it carries an id and a status, and the list query projects twenty
 *    joined columns. Appending the notification would put a half-populated row on the
 *    timeline; invalidating refetches the real one, and React Query dedupes the burst.
 */

export const keys = {
  events: (p: unknown) => ["events", p] as const,
  event: (id: string) => ["event", id] as const,
  eventSummary: (p: unknown) => ["eventSummary", p] as const,
  cameras: (p: unknown) => ["cameras", p] as const,
  camera: (ref: string) => ["camera", ref] as const,
  cameraTimeline: (ref: string, p: unknown) => ["cameraTimeline", ref, p] as const,
  systems: () => ["systems"] as const,
  districts: () => ["districts"] as const,
  tracks: (p: unknown) => ["tracks", p] as const,
  liveDetections: (ref: string) => ["liveDetections", ref] as const,
  track: (id: string) => ["track", id] as const,
  reads: (id: string) => ["reads", id] as const,
  alerts: (p: unknown) => ["alerts", p] as const,
  alert: (id: string) => ["alert", id] as const,
  alertSummary: () => ["alertSummary"] as const,
  incidents: (p: unknown) => ["incidents", p] as const,
  incident: (id: string) => ["incident", id] as const,
  notes: (p: unknown) => ["notes", p] as const,
  bookmarks: (p: unknown) => ["bookmarks", p] as const,
  tags: () => ["tags"] as const,
  zones: (p: unknown) => ["zones", p] as const,
  zoneRules: () => ["zoneRules"] as const,
  watchlist: () => ["watchlist"] as const,
  facets: () => ["facets"] as const,
  search: (body: unknown) => ["search", body] as const,
  graph: () => ["graph"] as const,
  neighbours: (ref: string) => ["neighbours", ref] as const,
  pursuits: (p: unknown) => ["pursuits", p] as const,
  pursuit: (id: string) => ["pursuit", id] as const,
  evidence: (p: unknown) => ["evidence", p] as const,
  health: () => ["health"] as const,
  playbooks: () => ["playbooks"] as const,
  registration: (id: string) => ["registration", id] as const,
};

const MINUTE = 60_000;

// ── Reads ───────────────────────────────────────────────────────────────────

export const useHealth = () =>
  useQuery({ queryKey: keys.health(), queryFn: () => api.get<any>("/health"), refetchInterval: 30_000 });

/** The job catalogue. Served rather than hard-coded so the console, the API docs
 *  and the audit log all name the same set of jobs. It changes only on deploy. */
export const usePlaybooks = () =>
  useQuery({
    queryKey: keys.playbooks(),
    queryFn: () => api.get<{ playbooks: PlaybookSpec[]; count: number }>("/playbooks"),
    staleTime: 10 * 60_000,
  });

/** What a plate is registered to, and whether the footage agrees.
 *  Disabled until a track has a plate — there is nothing to look up otherwise. */
export const useRegistration = (trackId?: string, plate?: string | null) =>
  useQuery({
    queryKey: keys.registration(trackId ?? ""),
    queryFn: () => api.get<RegistrationCheck>(`/tracks/${trackId}/registration`),
    enabled: Boolean(trackId && plate),
    staleTime: 5 * 60_000,
  });

export const useEvents = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.events(params),
    queryFn: ({ signal }) => api.get<Page<EventRow>>("/events", params, signal),
    placeholderData: keepPreviousData,
    staleTime: 10_000,
  });

export const useEvent = (id: string | undefined) =>
  useQuery({
    queryKey: keys.event(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/events/${id}`, undefined, signal),
    enabled: !!id,
  });

export const useEventSummary = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.eventSummary(params),
    queryFn: ({ signal }) => api.get<any>("/events/summary", params, signal),
    staleTime: 20_000,
  });

export const useCameras = (params: Record<string, unknown> = {}) =>
  useQuery({
    queryKey: keys.cameras(params),
    queryFn: ({ signal }) => api.get<Page<Camera>>("/cameras", { limit: 200, ...params }, signal),
    staleTime: 5 * MINUTE,   // the estate changes on a sync, not on a navigation
  });

export const useCamera = (ref: string | undefined) =>
  useQuery({
    queryKey: keys.camera(ref ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/cameras/${ref}`, undefined, signal),
    enabled: !!ref,
    staleTime: MINUTE,
  });

export const useCameraTimeline = (ref: string | undefined, params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.cameraTimeline(ref ?? "", params),
    queryFn: ({ signal }) => api.get<any>(`/cameras/${ref}/timeline`, params, signal),
    enabled: !!ref,
    placeholderData: keepPreviousData,
  });

export const useSystems = () =>
  useQuery({
    queryKey: keys.systems(),
    queryFn: () => api.get<{ systems: SourceSystem[]; count: number }>("/cameras/systems"),
    staleTime: 5 * MINUTE,
  });

export const useDistricts = () =>
  useQuery({ queryKey: keys.districts(), queryFn: () => api.get<any>("/cameras/districts"), staleTime: 5 * MINUTE });

export const useTracks = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.tracks(params),
    queryFn: ({ signal }) => api.get<Page<TrackRow>>("/tracks", params, signal),
    placeholderData: keepPreviousData,
  });

/**
 * The detections the overlay draws, polled while a camera is on screen.
 *
 * Polled rather than pushed. The SSE bus carries events, alerts and pursuits —
 * things an operator is notified about — and putting every detector box on it
 * would mean thousands of frames a minute crossing a channel whose back-pressure
 * policy is to drop the oldest. Dropping boxes silently is the one behaviour an
 * overlay must not have; a poll that misses simply draws the next second.
 *
 * `last_minutes: 1` keeps the response to what could plausibly still be on
 * screen. Without it the newest rows are whatever the last indexing pass wrote —
 * days old on this estate — which the overlay would fetch every second and
 * discard every second.
 *
 * `enabled` is how a camera that is not being watched costs nothing, and it is
 * why this is not simply `useDetections`: a poll that outlives the view it
 * belongs to is a load generator, which this player has already been once.
 */
export const useLiveDetections = (ref: string | undefined, enabled: boolean) =>
  useQuery({
    queryKey: keys.liveDetections(ref ?? ""),
    queryFn: ({ signal }) =>
      api.get<Page<DetectionRow>>(
        "/detections", { camera: ref, last_minutes: 1, limit: 100 }, signal),
    enabled: !!ref && enabled,
    refetchInterval: 1000,
    // A box that arrives late is worse than no box, so a slow response is not
    // worth retrying — the next tick is already a second away.
    retry: false,
    staleTime: 0,
  });

export const useTrack = (id: string | undefined) =>
  useQuery({
    queryKey: keys.track(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/tracks/${id}`, undefined, signal),
    enabled: !!id,
  });

export const useTrackReads = (id: string | undefined) =>
  useQuery({
    queryKey: keys.reads(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/tracks/${id}/reads`, undefined, signal),
    enabled: !!id,
  });

export const useAlerts = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.alerts(params),
    queryFn: ({ signal }) => api.get<Page<AlertRow>>("/alerts", params, signal),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
  });

export const useAlert = (id: string | undefined) =>
  useQuery({
    queryKey: keys.alert(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/alerts/${id}`, undefined, signal),
    enabled: !!id,
  });

export const useAlertSummary = () =>
  useQuery({ queryKey: keys.alertSummary(), queryFn: () => api.get<any>("/alerts/summary"), staleTime: 10_000 });

export const useIncidents = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.incidents(params),
    queryFn: ({ signal }) => api.get<Page<Incident>>("/incidents", params, signal),
    placeholderData: keepPreviousData,
  });

export const useIncident = (id: string | undefined) =>
  useQuery({
    queryKey: keys.incident(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/incidents/${id}`, undefined, signal),
    enabled: !!id,
  });

export const useNotes = (params: Record<string, unknown>) =>
  useQuery({
    queryKey: keys.notes(params),
    queryFn: ({ signal }) => api.get<Page<Note>>("/notes", params, signal),
  });

export const useBookmarks = (params: Record<string, unknown> = {}) =>
  useQuery({ queryKey: keys.bookmarks(params), queryFn: ({ signal }) => api.get<any>("/bookmarks", params, signal) });

export const useTags = () =>
  useQuery({ queryKey: keys.tags(), queryFn: () => api.get<{ tags: Tag[] }>("/tags"), staleTime: MINUTE });

export const useZones = (params: Record<string, unknown> = {}) =>
  useQuery({ queryKey: keys.zones(params), queryFn: ({ signal }) => api.get<{ zones: Zone[]; count: number }>("/zones", params, signal) });

export const useZoneRules = () =>
  useQuery({ queryKey: keys.zoneRules(), queryFn: () => api.get<any>("/zones/rules"), staleTime: Infinity });

export const useWatchlist = () =>
  useQuery({ queryKey: keys.watchlist(), queryFn: () => api.get<any>("/watchlist") });

export const useFacets = () =>
  useQuery({ queryKey: keys.facets(), queryFn: () => api.get<Facets>("/search/facets"), staleTime: 5 * MINUTE });

export const useGraph = () =>
  useQuery({ queryKey: keys.graph(), queryFn: () => api.get<any>("/investigate/graph"), staleTime: MINUTE });

export const useNeighbours = (ref: string | undefined) =>
  useQuery({
    queryKey: keys.neighbours(ref ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/investigate/neighbours/${ref}`, undefined, signal),
    enabled: !!ref,
    staleTime: MINUTE,
  });

export const usePursuits = (params: Record<string, unknown> = {}) =>
  useQuery({ queryKey: keys.pursuits(params), queryFn: ({ signal }) => api.get<any>("/pursuits", params, signal) });

export const usePursuit = (id: string | undefined) =>
  useQuery({
    queryKey: keys.pursuit(id ?? ""),
    queryFn: ({ signal }) => api.get<any>(`/pursuits/${id}`, undefined, signal),
    enabled: !!id,
  });

export const useEvidence = (params: Record<string, unknown> = {}) =>
  useQuery({ queryKey: keys.evidence(params), queryFn: ({ signal }) => api.get<any>("/evidence", params, signal) });

export const useEvidenceItem = (id: string | undefined) =>
  useQuery({
    queryKey: ["evidenceItem", id ?? ""],
    queryFn: ({ signal }) => api.get<any>(`/evidence/${id}`, undefined, signal),
    enabled: !!id,
  });

/** Recomputes the manifest digest server-side and diffs the sealed rows against the live
 *  ones. Never cached: the whole point is what is true at the moment it is asked, and a
 *  stale "intact" is the one answer a chain-of-custody check must not give. */
export const useEvidenceVerify = (id: string | undefined) =>
  useQuery({
    queryKey: ["evidenceVerify", id ?? ""],
    queryFn: ({ signal }) => api.get<any>(`/evidence/${id}/verify`, undefined, signal),
    enabled: !!id,
    staleTime: 0,
    gcTime: 0,
  });

/** Search is a POST because the filter set does not fit a query string, so it is a
 *  query keyed on the body rather than a mutation. `enabled` keeps it from firing on an
 *  empty form — 50k detections deserve a filter first. */
export const useSearch = (body: Record<string, unknown> | null) =>
  useQuery({
    queryKey: keys.search(body),
    queryFn: ({ signal }) => api.post<any>("/search", body ?? {}, undefined, signal),
    enabled: !!body,
    placeholderData: keepPreviousData,
  });

// ── Writes ──────────────────────────────────────────────────────────────────
// Every mutation invalidates by prefix rather than patching a cache entry by hand.
// A triage transition changes the row, the summary tiles, the camera's open count and
// possibly an incident's event list; enumerating those at each call site is how one of
// them ends up stale and an operator sees a number that disagrees with the list under it.

const bump = (qc: QueryClient, ...prefixes: string[]) =>
  prefixes.forEach((p) => qc.invalidateQueries({ queryKey: [p] }));

export function useTriage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: string; note?: string }) =>
      api.patch<any>(`/events/${id}`, { status, note: note || null }),
    onSuccess: (_d, v) => {
      bump(qc, "events", "eventSummary", "cameras", "camera", "incident");
      qc.invalidateQueries({ queryKey: keys.event(v.id) });
    },
  });
}

export function useReviewAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: string; note?: string }) =>
      api.patch<any>(`/alerts/${id}`, { status, note: note || null }),
    // The mirrored event moves in step (tr_alert_mirror), so the timeline is stale too.
    onSuccess: () => bump(qc, "alerts", "alertSummary", "events", "eventSummary", "alert"),
  });
}

export function useApplyTags() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ eventId, tags }: { eventId: string; tags: string[] }) =>
      api.post<any>(`/events/${eventId}/tags`, { tags }),
    // `tags` too: apply-by-name creates on first use, so the vocabulary itself grew.
    onSuccess: () => bump(qc, "events", "event", "tags"),
  });
}

export function useRemoveTag() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ eventId, tag }: { eventId: string; tag: string }) =>
      api.del<void>(`/events/${eventId}/tags/${encodeURIComponent(tag)}`),
    onSuccess: () => bump(qc, "events", "event", "tags"),
  });
}

export function useAddNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { target_type: string; target_id: string; body: string }) =>
      api.post<Note>("/notes", b),
    // Both sides of the alert/event mirror show the note, so both are invalidated.
    onSuccess: () => bump(qc, "notes", "event", "events", "alert", "alerts", "incident"),
  });
}

export function useDeleteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del<void>(`/notes/${id}`),
    onSuccess: () => bump(qc, "notes", "event", "events", "alert", "alerts", "incident"),
  });
}

export function useToggleBookmark() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ target_type, target_id, label, on }:
      { target_type: string; target_id: string; label?: string; on: boolean }) =>
      on
        ? api.post<any>("/bookmarks", { target_type, target_id, label: label ?? "" })
        : api.del<void>("/bookmarks", { target_type, target_id }),
    onSuccess: () => bump(qc, "bookmarks", "events", "event"),
  });
}

export function useCreateIncident() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { title: string; description?: string; priority?: string; event_ids?: string[] }) =>
      api.post<Incident>("/incidents", b),
    onSuccess: () => bump(qc, "incidents", "events", "event"),
  });
}

export function useUpdateIncident() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...b }: { id: string } & Record<string, unknown>) =>
      api.patch<Incident>(`/incidents/${id}`, b),
    onSuccess: (_d, v) => { bump(qc, "incidents"); qc.invalidateQueries({ queryKey: keys.incident(v.id) }); },
  });
}

export function useAddIncidentEvents() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, event_ids, note }: { id: string; event_ids: string[]; note?: string }) =>
      api.post<any>(`/incidents/${id}/events`, { event_ids, note: note || null }),
    onSuccess: (_d, v) => {
      bump(qc, "incidents", "events");
      qc.invalidateQueries({ queryKey: keys.incident(v.id) });
    },
  });
}

export function useRemoveIncidentEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, eventId }: { id: string; eventId: string }) =>
      api.del<void>(`/incidents/${id}/events/${eventId}`),
    onSuccess: (_d, v) => {
      bump(qc, "incidents", "events");
      qc.invalidateQueries({ queryKey: keys.incident(v.id) });
    },
  });
}

export function useReorderIncident() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, event_ids }: { id: string; event_ids: string[] }) =>
      api.post<any>(`/incidents/${id}/reorder`, { event_ids }),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: keys.incident(v.id) }),
  });
}

export function useCreateZone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: Record<string, unknown>) => api.post<Zone>("/zones", b),
    onSuccess: () => bump(qc, "zones", "cameras", "camera"),
  });
}

export function useUpdateZone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...b }: { id: string } & Record<string, unknown>) =>
      api.patch<Zone>(`/zones/${id}`, b),
    onSuccess: () => bump(qc, "zones"),
  });
}

export function useDeleteZone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del<void>(`/zones/${id}`),
    onSuccess: () => bump(qc, "zones", "cameras", "camera"),
  });
}

export function useAddWatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ backfill, ...b }: { type: string; value?: string; reason: string; backfill?: boolean }) =>
      api.post<any>("/watchlist", b, backfill ? { backfill: true } : undefined),
    // backfill=true raises alerts from history, so the queue and the timeline both move.
    onSuccess: () => bump(qc, "watchlist", "alerts", "alertSummary", "events", "eventSummary"),
  });
}

export function useRemoveWatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del<void>(`/watchlist/${id}`),
    onSuccess: () => bump(qc, "watchlist", "alerts", "alertSummary"),
  });
}

export function useCreateEvidence() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: Record<string, unknown>) => api.post<any>("/evidence", b),
    onSuccess: () => bump(qc, "evidence"),
  });
}

export function useNextHop() {
  return useMutation({ mutationFn: (b: Record<string, unknown>) => api.post<any>("/investigate/next-hop", b) });
}

export function useStartDragnet() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: Record<string, unknown>) => api.post<any>("/pursuits", b),
    onSuccess: () => bump(qc, "pursuits"),
  });
}

export function useReviewSighting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirmed }: { id: string; confirmed: boolean; pursuitId: string }) =>
      api.patch<any>(`/pursuits/sightings/${id}`, { confirmed }),
    onSuccess: (_d, v) => {
      bump(qc, "pursuits");
      qc.invalidateQueries({ queryKey: keys.pursuit(v.pursuitId) });
    },
  });
}

export function useRerunPursuit() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<any>(`/pursuits/${id}/rerun`),
    onSuccess: (_d, id) => { bump(qc, "pursuits"); qc.invalidateQueries({ queryKey: keys.pursuit(id) }); },
  });
}

export function useRebuildGraph() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<any>("/investigate/graph/rebuild"),
    onSuccess: () => bump(qc, "graph", "neighbours"),
  });
}

// ── Live wiring ─────────────────────────────────────────────────────────────

/**
 * Turn bus notifications into cache invalidations.
 *
 * Mounted once, in the shell. The debounce is the point: `backfill=true` on a watchlist
 * entry raised four alerts in one statement, and a burst of four NOTIFYs arriving inside
 * a millisecond of each other must not become four refetches of a twenty-column join.
 */
export function useLiveSync(): void {
  const qc = useQueryClient();
  useEffect(() => {
    let pending = new Set<string>();
    let timer: number | undefined;

    const flush = () => {
      timer = undefined;
      const prefixes = [...pending];
      pending = new Set();
      prefixes.forEach((p) => qc.invalidateQueries({ queryKey: [p] }));
    };
    const queueBump = (...prefixes: string[]) => {
      prefixes.forEach((p) => pending.add(p));
      if (timer === undefined) timer = window.setTimeout(flush, 300);
    };

    const off = subscribe((topic) => {
      if (topic === EVENTS) queueBump("events", "eventSummary", "event", "cameras", "camera", "cameraTimeline");
      else if (topic === ALERTS) queueBump("alerts", "alertSummary", "alert", "events", "eventSummary");
      else if (topic === PURSUITS) queueBump("pursuits", "pursuit");
    });
    return () => {
      off();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [qc]);
}
