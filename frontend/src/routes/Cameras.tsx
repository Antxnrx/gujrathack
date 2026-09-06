import { lazy, Suspense, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useCameras, useDistricts, useSystems } from "../lib/queries";
import { ago, num, stamp, titleCase } from "../lib/format";
import { Empty, ErrorBox, Pill, Skeleton } from "../components/ui";
import { useDebounced } from "../components/Filters";

const CameraMap = lazy(() => import("../components/CameraMap"));

/**
 * Feature 2 — camera-wise indexing, and the Model 2 federation view.
 *
 * Grouped by `source_system` rather than by district by default, because the question
 * this page answers for the evaluators is "which departmental systems is this thing
 * actually reading from", and the answer has to come from the estate itself. The column
 * is `cameras.metadata->>'source_system'`, populated per camera at sync time, which is
 * also the join point where a second VMS attaches without anything else changing.
 *
 * Filtering happens client-side on purpose: the estate is 30 rows and cached for five
 * minutes, so a round trip per keystroke would be slower and no more correct.
 */
export default function Cameras() {
  const nav = useNavigate();
  const cams = useCameras();
  const systems = useSystems();
  const districts = useDistricts();
  const [qRaw, setQRaw] = useState("");
  const q = useDebounced(qRaw, 150).toLowerCase();
  const [system, setSystem] = useState("");
  const [district, setDistrict] = useState("");
  const [sort, setSort] = useState<"id" | "busy" | "open">("id");

  const rows = useMemo(() => {
    let xs = cams.data?.items ?? [];
    if (system) xs = xs.filter((c) => (c.metadata?.source_system ?? "") === system);
    if (district) xs = xs.filter((c) => c.district === district);
    if (q) xs = xs.filter((c) =>
      `${c.external_id} ${c.name} ${c.district ?? ""} ${c.area ?? ""} ${c.camera_uid ?? ""}`.toLowerCase().includes(q));
    const by = {
      id: (a: typeof xs[0], b: typeof xs[0]) => a.external_id.localeCompare(b.external_id),
      busy: (a: typeof xs[0], b: typeof xs[0]) => b.detection_count - a.detection_count,
      open: (a: typeof xs[0], b: typeof xs[0]) => b.open_event_count - a.open_event_count,
    }[sort];
    return [...xs].sort(by);
  }, [cams.data, q, system, district, sort]);

  if (cams.isError) return <div className="view"><ErrorBox error={cams.error} what="Camera index" /></div>;

  return (
    <div className="view col" style={{ gap: 14 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <input placeholder="Camera, area, district, UID…" value={qRaw}
               onChange={(e) => setQRaw(e.target.value)} style={{ width: 240 }} aria-label="Filter cameras" />
        <select value={system} onChange={(e) => setSystem(e.target.value)} aria-label="Source system">
          <option value="">All source systems</option>
          {(systems.data?.systems ?? []).map((s) => (
            <option key={s.source_system} value={s.source_system}>{s.source_system} ({s.cameras})</option>
          ))}
        </select>
        <select value={district} onChange={(e) => setDistrict(e.target.value)} aria-label="District">
          <option value="">All districts</option>
          {(districts.data?.districts ?? []).map((d: any) => (
            <option key={d.district} value={d.district}>{d.district} ({d.cameras})</option>
          ))}
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as any)} aria-label="Sort">
          <option value="id">By id</option>
          <option value="busy">Most indexed</option>
          <option value="open">Most open events</option>
        </select>
        <span className="right dim small">{rows.length} of {cams.data?.total ?? 0}</span>
      </div>

      <section className="card" style={{ height: 300 }}>
        <Suspense fallback={<Skeleton rows={4} />}>
          <CameraMap cameras={rows} onSelect={(ext) => nav(`/cameras/${ext}`)} />
        </Suspense>
      </section>

      {cams.isLoading && <Skeleton rows={8} />}
      {rows.length === 0 && !cams.isLoading && <Empty>No camera matches.</Empty>}

      <div className="grid g3">
        {rows.map((c) => (
          <Link key={c.id} to={`/cameras/${c.external_id}`} className="card" style={{ color: "inherit" }}>
            <div className="body col" style={{ gap: 7 }}>
              <div className="row">
                <span className="mono">{c.external_id}</span>
                <span className="right row" style={{ gap: 4 }}>
                  {c.open_event_count > 0 && <span className="badge hot">{c.open_event_count} open</span>}
                  {c.has_stream ? <Pill k="ok">feed</Pill> : <Pill>no feed</Pill>}
                </span>
              </div>
              <div className="trunc" style={{ fontSize: 13 }}>{c.name}</div>
              <div className="tiny faint trunc">
                {[c.area, c.district].filter(Boolean).join(", ") || "location unknown"}
                {c.geocode_precision ? ` · ${c.geocode_precision}` : ""}
              </div>
              <div className="row tiny" style={{ gap: 5 }}>
                <Pill k="accent">{titleCase(c.department)}</Pill>
                <span className="faint trunc">{c.metadata?.source_system ?? "—"}</span>
              </div>
              <dl className="kv tiny">
                <dt>Detections</dt><dd>{num(c.detection_count)}</dd>
                <dt>Tracks</dt><dd>{num(c.track_count)}</dd>
                <dt>Plates read</dt><dd>{num(c.plate_count)}</dd>
                <dt>Events</dt><dd>{num(c.event_count)}</dd>
                <dt>Zones</dt><dd>{num(c.zone_count)}</dd>
                <dt>Indexed to</dt><dd>{c.last_ts ? ago(c.last_ts) : "never"}</dd>
              </dl>
              {c.first_ts && (
                <div className="tiny faint">{stamp(c.first_ts)} → {stamp(c.last_ts)}</div>
              )}
              {Object.keys(c.class_mix ?? {}).length > 0 && (
                <div className="row wrap tiny" style={{ gap: 4 }}>
                  {Object.entries(c.class_mix).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => (
                    <span key={k} className="pill">{k} {num(v)}</span>
                  ))}
                </div>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
