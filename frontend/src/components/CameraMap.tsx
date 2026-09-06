import { useEffect, useRef } from "react";
import type { Camera } from "../lib/types";

/**
 * Leaflet directly, no react-leaflet.
 *
 * The wrapper is pleasant and costs another dependency plus a component tree that
 * re-renders markers on every parent render. Here the map is created once and markers
 * are diffed in an effect, so panning the map does not touch React at all — which is
 * the difference between a map that feels native and one that stutters when the event
 * bus pushes an update.
 *
 * Tiles come from OSM and the demo may be offline. On tile failure the container keeps
 * its dark background and the markers stay exactly where they are: the geometry of the
 * camera network is the information, and it does not need a basemap to be readable.
 */

type Props = {
  cameras: Camera[];
  selected?: string | null;
  onSelect?: (externalId: string) => void;
  /** Drawn as dashed polylines — the transition graph, when a view has one. */
  links?: { from: [number, number]; to: [number, number]; strong: boolean }[];
  fit?: boolean;
};

/**
 * Build the link list from camera external ids, which is what every caller actually has —
 * a route is a sequence of camera references, not of coordinate pairs. Cameras without a
 * geocode are dropped rather than drawn at (0,0), because a line into the Gulf of Guinea
 * is worse than no line.
 */
export function linksFromRefs(
  cameras: { external_id: string; lat: number | null; lng: number | null }[],
  pairs: { src?: string; dst?: string; evidence?: string }[],
): { from: [number, number]; to: [number, number]; strong: boolean }[] {
  const at = new Map(cameras.filter((c) => c.lat != null && c.lng != null)
    .map((c) => [c.external_id, [c.lat as number, c.lng as number] as [number, number]]));
  const out: { from: [number, number]; to: [number, number]; strong: boolean }[] = [];
  for (const p of pairs) {
    const a = p.src ? at.get(p.src) : undefined;
    const b = p.dst ? at.get(p.dst) : undefined;
    if (a && b) out.push({ from: a, to: b, strong: p.evidence === "confirmed" || p.evidence === "plate" });
  }
  return out;
}

const COLOUR: Record<string, string> = {
  police: "#38bdf8",
  municipal: "#a78bfa",
  highways: "#fbbf24",
};

export default function CameraMap({ cameras, selected, onSelect, links, fit = true }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<import("leaflet").Map | null>(null);
  const layer = useRef<import("leaflet").LayerGroup | null>(null);
  const L = useRef<typeof import("leaflet") | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    let dead = false;
    (async () => {
      const mod = await import("leaflet");
      await import("leaflet/dist/leaflet.css");
      if (dead || !host.current || map.current) return;
      L.current = mod;
      const m = mod.map(host.current, {
        zoomControl: true,
        attributionControl: false,
        preferCanvas: true,          // 30 circle markers on canvas, not 30 DOM nodes
      }).setView([23.03, 72.58], 11);
      mod.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18,
        // Deliberately quiet on failure: an offline demo shows markers on the dark
        // panel rather than a grid of broken-tile icons.
        errorTileUrl: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
      }).addTo(m);
      layer.current = mod.layerGroup().addTo(m);
      map.current = m;
      // Leaflet measures its container on creation; inside a grid pane that measurement
      // is often taken before layout settles, and the map renders one tile wide.
      window.setTimeout(() => m.invalidateSize(), 0);
    })();
    return () => {
      dead = true;
      map.current?.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const mod = L.current, m = map.current, lg = layer.current;
    if (!mod || !m || !lg) return;
    lg.clearLayers();

    for (const link of links ?? []) {
      mod.polyline([link.from, link.to], {
        color: link.strong ? "#34d399" : "#334155",
        weight: link.strong ? 2 : 1,
        opacity: link.strong ? 0.8 : 0.5,
        dashArray: link.strong ? undefined : "3 5",
      }).addTo(lg);
    }

    const pts: [number, number][] = [];
    for (const c of cameras) {
      if (c.lat == null || c.lng == null) continue;
      pts.push([c.lat, c.lng]);
      const on = selected === c.external_id;
      const hot = c.open_event_count > 0;
      mod.circleMarker([c.lat, c.lng], {
        radius: on ? 9 : hot ? 7 : 5,
        color: on ? "#ffffff" : "#0b0f14",
        weight: on ? 2 : 1,
        fillColor: hot ? "#f43f5e" : (COLOUR[c.department ?? ""] ?? "#64748b"),
        fillOpacity: 0.95,
        className: "cam-pin",
      })
        .bindTooltip(
          `<b>${c.external_id}</b> ${c.name}<br>${c.district ?? ""} · ${c.detection_count.toLocaleString("en-IN")} detections` +
            (hot ? `<br><b style="color:#f43f5e">${c.open_event_count} open</b>` : ""),
          { direction: "top", opacity: 0.95 },
        )
        .on("click", () => onSelectRef.current?.(c.external_id))
        .addTo(lg);
    }
    if (fit && pts.length > 1) m.fitBounds(mod.latLngBounds(pts).pad(0.15), { animate: false });
  }, [cameras, links, selected, fit]);

  return <div className="mapwrap" ref={host} role="application" aria-label="Camera map" />;
}
