import { Suspense, lazy, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useCameras, useDistricts, useSystems } from "../lib/queries";
import { ago, num, titleCase } from "../lib/format";
import { Bar, Empty, ErrorBox, Help, Pill, Skeleton } from "../components/ui";
import { useDebounced } from "../components/Filters";

const CameraMap = lazy(() => import("../components/CameraMap"));

/**
 * Feature 2 — the camera estate, as a grid you can sweep.
 *
 * Two panes that scroll independently. The filters, the map and the estate
 * summary live on the left and stay put; the grid scrolls on the right. That is
 * the whole reason this is not one `.view`: thirty tiles is four screens of
 * scrolling, and in the previous layout the filters and the map went off the top
 * with them, so narrowing a search meant scrolling back up to find the control
 * that narrows it.
 *
 * Density is the operator's choice. Four across sweeps an estate for the one red
 * tile; two across is for reading the figures. Neither is right for both jobs.
 *
 * Filtering is client-side on purpose: the estate is thirty rows cached for five
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
  const [sort, setSort] = useState<"id" | "busy" | "open" | "plates">("id");
  const [density, setDensity] = useState<2 | 3 | 4>(3);
  const [onlyOpen, setOnlyOpen] = useState(false);

  const rows = useMemo(() => {
    let xs = cams.data?.items ?? [];
    if (system) xs = xs.filter((c) => (c.metadata?.source_system ?? "") === system);
    if (district) xs = xs.filter((c) => c.district === district);
    if (onlyOpen) xs = xs.filter((c) => c.open_event_count > 0);
    if (q) xs = xs.filter((c) =>
      `${c.external_id} ${c.name} ${c.district ?? ""} ${c.area ?? ""} ${c.camera_uid ?? ""}`
        .toLowerCase().includes(q));
    const by = {
      id: (a: typeof xs[0], b: typeof xs[0]) => a.external_id.localeCompare(b.external_id),
      busy: (a: typeof xs[0], b: typeof xs[0]) => b.detection_count - a.detection_count,
      open: (a: typeof xs[0], b: typeof xs[0]) => b.open_event_count - a.open_event_count,
      plates: (a: typeof xs[0], b: typeof xs[0]) => b.plate_count - a.plate_count,
    }[sort];
    return [...xs].sort(by);
  }, [cams.data, q, system, district, sort, onlyOpen]);

  const all = cams.data?.items ?? [];
  const estate = useMemo(() => ({
    total: all.length,
    withFeed: all.filter((c) => c.has_stream).length,
    withOpen: all.filter((c) => c.open_event_count > 0).length,
    indexed: all.filter((c) => c.detection_count > 0).length,
  }), [all]);

  if (cams.isError) return <div className="view"><ErrorBox error={cams.error} what="Camera index" /></div>;

  return (
    <div className="workspace">
      {/* Stays put while the grid scrolls. */}
      <aside className="ws-side">
        <div className="col" style={{ gap: 7 }}>
          <label className="field" htmlFor="cam-q">Search</label>
          <input id="cam-q" placeholder="Camera, area, district, UID…" value={qRaw}
                 onChange={(e) => setQRaw(e.target.value)} />

          <label className="field" htmlFor="cam-sys">Source system</label>
          <select id="cam-sys" value={system} onChange={(e) => setSystem(e.target.value)}>
            <option value="">All source systems</option>
            {(systems.data?.systems ?? []).map((s) => (
              <option key={s.source_system} value={s.source_system}>
                {s.source_system} ({s.cameras})
              </option>
            ))}
          </select>

          <label className="field" htmlFor="cam-dist">District</label>
          <select id="cam-dist" value={district} onChange={(e) => setDistrict(e.target.value)}>
            <option value="">All districts</option>
            {(districts.data?.districts ?? []).map((d: any) => (
              <option key={d.district} value={d.district}>{d.district} ({d.cameras})</option>
            ))}
          </select>

          <label className="field" htmlFor="cam-sort">Order</label>
          <select id="cam-sort" value={sort} onChange={(e) => setSort(e.target.value as any)}>
            <option value="id">By id</option>
            <option value="busy">Most indexed</option>
            <option value="open">Most open events</option>
            <option value="plates">Most plates read</option>
          </select>

          <label className="check">
            <input type="checkbox" checked={onlyOpen}
                   onChange={(e) => setOnlyOpen(e.target.checked)} />
            Only cameras with open events
          </label>
        </div>

        <section className="card">
          <header>
            <h2 className="grow">Estate</h2>
            <Help>
              Counted from the registry’s own rows. “Indexed” is separate from
              “has a feed”: indexing is a batch pass over recorded footage, so a
              camera can be streaming and contribute nothing to search.
            </Help>
          </header>
          <div className="body col" style={{ gap: 8 }}>
            <SideStat label="Cameras" n={estate.total} max={estate.total} />
            <SideStat label="With a live feed" n={estate.withFeed} max={estate.total} tone="ok" />
            <SideStat label="Indexed" n={estate.indexed} max={estate.total} tone="accent" />
            <SideStat label="With open events" n={estate.withOpen} max={estate.total} tone="warn" />
          </div>
        </section>

        <section className="card" style={{ height: 250, display: "flex", flexDirection: "column" }}>
          <header>
            <h2 className="grow">Where they are</h2>
            <Help>Shows only the cameras matching the filters above. Click a pin to open one.</Help>
          </header>
          <div style={{ flex: 1, minHeight: 0 }}>
            <Suspense fallback={<Skeleton rows={3} />}>
              <CameraMap cameras={rows} fit onSelect={(ext) => nav(`/cameras/${ext}`)} />
            </Suspense>
          </div>
        </section>
      </aside>

      {/* Scrolls on its own. */}
      <main className="ws-main">
        <div className="ws-bar">
          <b>{rows.length}</b>
          <span className="dim small">of {estate.total} cameras</span>
          <span className="grow" />
          <span className="tiny faint">Density</span>
          <div className="row" style={{ gap: 3 }}>
            {([4, 3, 2] as const).map((d) => (
              <button key={d} className={density === d ? "primary sm" : "ghost sm"}
                      onClick={() => setDensity(d)} title={`${d} across`}>{d}×</button>
            ))}
          </div>
        </div>

        {cams.isLoading && <Skeleton rows={8} />}
        {rows.length === 0 && !cams.isLoading && <Empty>No camera matches these filters.</Empty>}

        <div className={`cam-grid d${density}`}>
          {rows.map((c) => (
            <Link key={c.id} to={`/cameras/${c.external_id}`}
                  className={`cam-tile ${c.open_event_count > 0 ? "hot" : ""}`}>
              <div className="cam-tile-head">
                <i className={`feed-dot ${c.has_stream ? "on" : "off"}`}
                   title={c.has_stream ? "Live feed available" : "No feed configured"} />
                <span className="id">{c.external_id}</span>
                <span className="grow" />
                {c.open_event_count > 0 && <span className="badge hot">{c.open_event_count}</span>}
              </div>
              <div className="cam-tile-body">
                <div className="nm trunc" title={c.name}>{c.name}</div>
                <div className="tiny faint trunc">
                  {[c.area, c.district].filter(Boolean).join(", ") || "location unknown"}
                </div>
                <div className="cam-stats">
                  <Figure n={c.detection_count} l="detections" />
                  <Figure n={c.track_count} l="tracks" />
                  <Figure n={c.plate_count} l="plates" />
                </div>
                {density !== 4 && (
                  <>
                    <div className="row tiny" style={{ gap: 5 }}>
                      <Pill k="accent">{titleCase(c.department)}</Pill>
                      <span className="faint trunc">{c.metadata?.source_system ?? "—"}</span>
                    </div>
                    <div className="tiny faint">
                      {c.last_ts ? `Indexed to ${ago(c.last_ts)}` : "Never indexed"}
                      {c.zone_count ? ` · ${c.zone_count} zone${c.zone_count > 1 ? "s" : ""}` : ""}
                    </div>
                  </>
                )}
              </div>
            </Link>
          ))}
        </div>
      </main>
    </div>
  );
}

function Figure({ n, l }: { n: number; l: string }) {
  return <div className="cam-stat"><b>{num(n)}</b><span>{l}</span></div>;
}

function SideStat({ label, n, max, tone }:
  { label: string; n: number; max: number; tone?: "ok" | "warn" | "accent" }) {
  const colour = tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--warn)"
    : tone === "accent" ? "var(--accent)" : undefined;
  return (
    <div className="col" style={{ gap: 3 }}>
      <div className="row small"><span className="grow">{label}</span>
        <span className="mono faint">{n}</span></div>
      <Bar value={n} max={max || 1} color={colour} />
    </div>
  );
}
