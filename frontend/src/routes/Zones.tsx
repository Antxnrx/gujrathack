import { useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  useCameras, useCreateZone, useDeleteZone, useUpdateZone, useZoneRules, useZones,
} from "../lib/queries";
import { ago, num, titleCase } from "../lib/format";
import { Empty, ErrorBox, Modal, Pill, Skeleton } from "../components/ui";
import { useReport } from "../components/Toasts";

/**
 * Features 15–20 — the zone-scoped detectors, and the surface that configures them.
 *
 * A zone is not decoration. Four of the seven rules cannot fire at all without one:
 * `zone_intrusion` needs a restricted polygon, `wrong_way` needs a permitted bearing,
 * `crowd` needs an area to count inside, `abandoned_object` needs somewhere to watch.
 * `zones/rules` is the mapping, fetched rather than hardcoded here, so the list on screen
 * is the list the analytics pass actually runs.
 *
 * Coordinates are normalised 0–1, which is why the editor is a unit square rather than a
 * pixel canvas: the polygon has to survive the source feed changing resolution, and the
 * API rejects anything outside that range. The square is drawn without a frame behind it
 * because this deployment has no stored key frames — saying so is better than showing a
 * black rectangle that looks like a broken image.
 */

const KIND_HELP: Record<string, string> = {
  restricted: "Anything of a watched class that puts three tracked points inside this polygon raises an intrusion.",
  direction: "Traffic is expected to head along the bearing. A track outside the tolerance arc is wrong-way.",
  crowd: "People inside are counted per bucket; sustained counts over the threshold raise a crowd event.",
  abandon_watch: "A bag or case stationary here with no person within the clearance radius raises an abandonment.",
  count_line: "Counts crossings. Recorded as flow, not raised as an event.",
};

export default function Zones() {
  const [sp] = useSearchParams();
  const preset = sp.get("camera") ?? "";
  const report = useReport();
  const zones = useZones();
  const rules = useZoneRules();
  const cams = useCameras();
  const del = useDeleteZone();
  const upd = useUpdateZone();
  const [camera, setCamera] = useState(preset);
  const [drawing, setDrawing] = useState(false);

  const list = useMemo(() => {
    const xs = zones.data?.zones ?? [];
    return camera ? xs.filter((z: any) => z.camera_external_id === camera) : xs;
  }, [zones.data, camera]);

  const byKind = useMemo(() => {
    const m: Record<string, number> = {};
    (zones.data?.zones ?? []).forEach((z: any) => { m[z.kind] = (m[z.kind] ?? 0) + 1; });
    return m;
  }, [zones.data]);

  return (
    <div className="view col" style={{ gap: 14 }}>
      <section className="card">
        <header><b>Detector rules</b><span className="grow" />
          <span className="tiny faint">from /zones/rules — this is the set the analytics pass runs</span>
        </header>
        <div className="body grid g2">
          {(rules.data?.detectors ?? []).map((d: any) => (
            <div key={d.rule} className="col" style={{ gap: 3 }}>
              <div className="row" style={{ gap: 6 }}>
                <b className="small">{d.rule}</b>
                {d.feature && <span className="badge">feature {d.feature}</span>}
                {d.zone_kind
                  ? <Pill k={byKind[d.zone_kind] ? "ok" : "warn"}>
                      needs a {d.zone_kind} zone{byKind[d.zone_kind] ? ` · ${byKind[d.zone_kind]} drawn` : " · none drawn"}
                    </Pill>
                  : <Pill k="accent">frame-wide</Pill>}
              </div>
              <div className="tiny faint">{d.description}</div>
            </div>
          ))}
        </div>
      </section>

      <div className="row wrap" style={{ gap: 8 }}>
        <select value={camera} onChange={(e) => setCamera(e.target.value)} aria-label="Camera">
          <option value="">All cameras</option>
          {(cams.data?.items ?? []).map((c) => (
            <option key={c.id} value={c.external_id}>{c.external_id} — {c.name}</option>
          ))}
        </select>
        <button className="primary" onClick={() => setDrawing(true)}>Draw a zone</button>
        <span className="right small dim">{list.length} of {zones.data?.count ?? 0} zones</span>
      </div>

      {zones.isError && <ErrorBox error={zones.error} what="Zones" />}
      {zones.isLoading && <Skeleton rows={6} />}
      {list.length === 0 && !zones.isLoading && (
        <Empty>
          No zone {camera ? `on ${camera}` : "anywhere"}. Four of the seven detectors are inert
          until one exists.
        </Empty>
      )}

      <div className="grid g3">
        {list.map((z: any) => (
          <div key={z.id} className="card">
            <div className="body col" style={{ gap: 7 }}>
              <div className="row" style={{ gap: 6 }}>
                <Pill k={z.active ? "ok" : undefined}>{z.kind}</Pill>
                <span className="right tiny faint">{z.polygon.length} points</span>
              </div>
              <b className="small trunc" title={z.name}>{z.name}</b>
              <ZoneShape polygon={z.polygon} direction={z.direction_deg} tolerance={z.tolerance_deg} />
              <Link to={`/cameras/${z.camera_external_id}`} className="tiny mono">{z.camera_external_id}</Link>
              <div className="tiny faint">{KIND_HELP[z.kind] ?? ""}</div>
              <dl className="kv tiny">
                <dt>Feeds</dt><dd>{z.feeds_rule ?? "—"}</dd>
                {z.direction_deg != null && (
                  <>
                    <dt>Bearing</dt><dd>{z.direction_deg.toFixed(0)}° ±{z.tolerance_deg.toFixed(0)}°</dd>
                  </>
                )}
                <dt>Classes</dt><dd>{z.classes.length ? z.classes.join(", ") : "all"}</dd>
                <dt>Events</dt><dd>{num(z.event_count ?? 0)}</dd>
                {z.last_event_at && <><dt>Last</dt><dd>{ago(z.last_event_at)}</dd></>}
              </dl>
              <div className="row" style={{ gap: 6 }}>
                <button className="sm" onClick={() => upd.mutate({ id: z.id, active: !z.active }, {
                  onSuccess: () => report.ok(z.active ? "Zone disabled" : "Zone enabled"),
                  onError: (e: any) => report.err(e?.message ?? "Update failed"),
                })}>{z.active ? "Disable" : "Enable"}</button>
                <Link className="sm" to={`/timeline?event_type=${z.feeds_rule ?? ""}&camera=${z.camera_external_id}`}>Events</Link>
                <button className="danger sm right" onClick={() => {
                  if (!window.confirm(`Delete "${z.name}"? Events it already raised stay on the timeline.`)) return;
                  del.mutate(z.id, {
                    onSuccess: () => report.ok("Zone deleted"),
                    onError: (e: any) => report.err(e?.message ?? "Delete failed"),
                  });
                }}>Delete</button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {drawing && (
        <DrawModal camera={camera} cameras={cams.data?.items ?? []} onClose={() => setDrawing(false)}
                   onDone={() => { report.ok("Zone created — the next analytics pass will use it"); setDrawing(false); }}
                   onFail={(e) => report.err(e)} />
      )}
    </div>
  );
}

/** The stored polygon, rendered from normalised coordinates with no pixel arithmetic. */
function ZoneShape({ polygon, direction, tolerance }: {
  polygon: number[][]; direction?: number | null; tolerance?: number | null;
}) {
  const pts = polygon.map(([x, y]) => `${x},${y}`).join(" ");
  // Bearings are compass degrees (0 = north, clockwise); SVG y grows downward.
  const arrow = (deg: number, len: number) => {
    const r = ((90 - deg) * Math.PI) / 180;
    return [0.5 + Math.cos(r) * len, 0.5 - Math.sin(r) * len];
  };
  return (
    <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="zoneshape" aria-hidden>
      <rect x="0" y="0" width="1" height="1" fill="none" stroke="var(--line)" strokeWidth="0.006" />
      <polygon points={pts} fill="var(--accent)" fillOpacity="0.16"
               stroke="var(--accent)" strokeWidth="0.008" />
      {direction != null && (() => {
        const [x2, y2] = arrow(direction, 0.34);
        const tol = tolerance ?? 75;
        const [ax, ay] = arrow(direction - tol, 0.3);
        const [bx, by] = arrow(direction + tol, 0.3);
        return (
          <g>
            <path d={`M 0.5 0.5 L ${ax} ${ay} M 0.5 0.5 L ${bx} ${by}`}
                  stroke="var(--warn)" strokeWidth="0.005" strokeDasharray="0.02 0.02" />
            <line x1="0.5" y1="0.5" x2={x2} y2={y2} stroke="var(--warn)" strokeWidth="0.012" />
          </g>
        );
      })()}
    </svg>
  );
}

/**
 * Click to place vertices. Coordinates come straight from the click position divided by
 * the element's own box, so what is stored is what was drawn regardless of how big the
 * editor rendered.
 */
function DrawModal({ camera, cameras, onClose, onDone, onFail }: {
  camera: string; cameras: any[]; onClose: () => void; onDone: () => void; onFail: (e: string) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [cam, setCam] = useState(camera || (cameras[0]?.external_id ?? ""));
  const [name, setName] = useState("");
  const [kind, setKind] = useState("restricted");
  const [pts, setPts] = useState<number[][]>([]);
  const [dir, setDir] = useState("");
  const [tol, setTol] = useState("75");
  const [classes, setClasses] = useState("");
  const create = useCreateZone();

  const place = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = svg.current!.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width));
    const y = Math.min(1, Math.max(0, (e.clientY - box.top) / box.height));
    setPts((p) => [...p, [+x.toFixed(4), +y.toFixed(4)]]);
  };

  const submit = () => {
    if (pts.length < 3) return onFail("A zone needs at least three points");
    create.mutate(
      {
        camera_id: cam,
        name: name.trim() || `${titleCase(kind)} zone on ${cam}`,
        kind,
        polygon: pts,
        direction_deg: kind === "direction" && dir ? Number(dir) : null,
        tolerance_deg: Number(tol) || 75,
        classes: classes.split(",").map((s) => s.trim()).filter(Boolean),
        active: true,
      },
      { onSuccess: onDone, onError: (e: any) => onFail(e?.message ?? "Create failed") },
    );
  };

  return (
    <Modal title="Draw a zone" onClose={onClose}
           footer={<>
             <button className="ghost" onClick={() => setPts([])} disabled={!pts.length}>Clear points</button>
             <button className="ghost" onClick={onClose}>Cancel</button>
             <button className="primary" onClick={submit} disabled={pts.length < 3 || create.isPending}>
               {create.isPending ? "Saving…" : `Save ${pts.length}-point zone`}
             </button>
           </>}>
      <div className="row wrap" style={{ gap: 8 }}>
        <label className="field">Camera
          <select value={cam} onChange={(e) => setCam(e.target.value)}>
            {cameras.map((c) => <option key={c.id} value={c.external_id}>{c.external_id} — {c.name}</option>)}
          </select>
        </label>
        <label className="field">Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {["restricted", "direction", "crowd", "abandon_watch", "count_line"].map((k) =>
              <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <label className="field grow">Name
          <input value={name} onChange={(e) => setName(e.target.value)}
                 placeholder={`${titleCase(kind)} zone on ${cam}`} />
        </label>
      </div>

      <p className="tiny faint" style={{ margin: "6px 0" }}>{KIND_HELP[kind]}</p>

      {kind === "direction" && (
        <div className="row wrap" style={{ gap: 8 }}>
          <label className="field">Permitted bearing (° from north)
            <input type="number" min={0} max={359} value={dir} onChange={(e) => setDir(e.target.value)}
                   placeholder="e.g. 90 for eastbound" />
          </label>
          <label className="field">Tolerance (±°)
            <input type="number" min={0} max={180} value={tol} onChange={(e) => setTol(e.target.value)} />
          </label>
          <span className="tiny faint" style={{ maxWidth: 260 }}>
            At 180 every heading is inside the arc, which is a wrong-way rule switched off
            rather than a loose one. The API caps it there for that reason.
          </span>
        </div>
      )}

      <label className="field">Classes to watch (comma separated, blank = all)
        <input value={classes} onChange={(e) => setClasses(e.target.value)} placeholder="car, truck, person" />
      </label>

      <div className="zonedraw">
        <svg ref={svg} viewBox="0 0 1 1" preserveAspectRatio="none" onClick={place}>
          <rect x="0" y="0" width="1" height="1" fill="var(--bg-2)" />
          {[0.25, 0.5, 0.75].map((g) => (
            <g key={g}>
              <line x1={g} y1="0" x2={g} y2="1" stroke="var(--line-soft)" strokeWidth="0.003" />
              <line x1="0" y1={g} x2="1" y2={g} stroke="var(--line-soft)" strokeWidth="0.003" />
            </g>
          ))}
          {pts.length > 1 && (
            <polygon points={pts.map(([x, y]) => `${x},${y}`).join(" ")}
                     fill="var(--accent)" fillOpacity="0.18" stroke="var(--accent)" strokeWidth="0.007" />
          )}
          {pts.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r="0.012" fill="var(--accent)" />
          ))}
        </svg>
        <div className="tiny faint" style={{ marginTop: 5 }}>
          Click to place vertices — {pts.length} placed, three minimum. The frame is a unit
          square, not a still: no key frames are stored in this deployment, and the polygon is
          saved normalised so it survives the source feed changing resolution.
        </div>
        {pts.length > 0 && (
          <div className="tiny mono faint" style={{ marginTop: 4, wordBreak: "break-all" }}>
            {pts.map(([x, y]) => `[${x}, ${y}]`).join(" ")}
          </div>
        )}
      </div>
    </Modal>
  );
}
