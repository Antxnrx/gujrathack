import { lazy, Suspense } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAlertSummary, useCameras, useEventSummary, useEvents, useFacets, useSystems } from "../lib/queries";
import { ago, num, stamp, titleCase } from "../lib/format";
import { Bar, Empty, ErrorBox, Pill, SevDot, Skeleton, Stat, Status } from "../components/ui";

const CameraMap = lazy(() => import("../components/CameraMap"));

/**
 * The wall view. What is happening, where, and what has not been looked at yet.
 *
 * Everything on this page comes from an aggregate endpoint rather than from counting
 * rows in the browser: `/events/summary` is one query with three GROUP BYs against
 * indexed columns, and it replaced what would otherwise be a tile-per-request fan-out.
 */
export default function Dashboard() {
  const nav = useNavigate();
  const summary = useEventSummary({});
  const alerts = useAlertSummary();
  const cameras = useCameras();
  const systems = useSystems();
  const facets = useFacets();
  const recent = useEvents({ limit: 12, order: "recent" });

  if (summary.isError) return <div className="view"><ErrorBox error={summary.error} what="Dashboard" /></div>;

  const t = summary.data?.totals;
  const at = alerts.data?.totals;
  const cams = cameras.data?.items ?? [];
  const byType: { event_type: string; severity: string; status: string; n: number }[] = summary.data?.breakdown ?? [];

  const typeTotals = new Map<string, number>();
  for (const r of byType) typeTotals.set(r.event_type, (typeTotals.get(r.event_type) ?? 0) + Number(r.n));
  const types = [...typeTotals].sort((a, b) => b[1] - a[1]);
  const maxType = types[0]?.[1] ?? 1;
  const coverage = facets.data?.coverage;

  return (
    <div className="view col" style={{ gap: 16 }}>
      <div className="grid g4">
        <Stat n={num(t?.total)} l="Events" />
        <Stat n={num(t?.open)} l="Awaiting triage" tone={t?.open ? "warm" : undefined} />
        <Stat n={num(t?.critical)} l="Critical" tone={t?.critical ? "hot" : undefined} />
        <Stat n={num(at?.pending)} l="Alerts pending" tone={at?.pending ? "hot" : undefined} />
        <Stat n={num(t?.cameras_involved)} l="Cameras with events" tone="cool" />
        <Stat n={num(t?.false_positives)} l="False positives" />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(300px, 1fr)" }}>
        <section className="card" style={{ minHeight: 340, display: "flex", flexDirection: "column" }}>
          <header>
            <h2 className="grow">Camera network</h2>
            <span className="tiny faint">red = open events · click to open a feed</span>
          </header>
          <div style={{ flex: 1, minHeight: 300 }}>
            <Suspense fallback={<Skeleton rows={5} />}>
              <CameraMap cameras={cams} onSelect={(ext) => nav(`/cameras/${ext}`)} />
            </Suspense>
          </div>
        </section>

        <div className="col">
          {/* The Model 2 deliverable, on the front page: this is the count of distinct
              departmental systems the console is federating, read from the estate rather
              than asserted in a slide. */}
          <section className="card">
            <header><h2 className="grow">Connected systems</h2>
              <Pill k="accent">{systems.data?.count ?? "—"} sources</Pill>
            </header>
            <div className="body col" style={{ gap: 9 }}>
              {(systems.data?.systems ?? []).map((s) => (
                <div key={s.source_system} className="col" style={{ gap: 3 }}>
                  <div className="row small">
                    <span className="grow trunc">{s.source_system}</span>
                    <span className="mono faint tiny">{s.protocols?.join("/") || "—"}</span>
                    <span className="badge">{s.active}/{s.cameras}</span>
                  </div>
                  <Bar value={s.cameras} max={cams.length || 1} />
                  <div className="tiny faint">
                    {titleCase(s.department)} · {s.districts} districts
                    {s.example_uid ? ` · ${s.example_uid}` : ""}
                  </div>
                </div>
              ))}
              {systems.isLoading && <Skeleton rows={3} />}
            </div>
          </section>

          <section className="card">
            <header><h2 className="grow">Detector output</h2></header>
            <div className="body col" style={{ gap: 6 }}>
              {types.length === 0 && !summary.isLoading && <Empty>No events yet.</Empty>}
              {types.map(([type, n]) => (
                <Link key={type} to={`/timeline?event_type=${encodeURIComponent(type)}`}
                      className="col" style={{ gap: 3, color: "inherit" }}>
                  <div className="row small">
                    <span className="grow">{titleCase(type)}</span>
                    <span className="mono faint">{num(n)}</span>
                  </div>
                  <Bar value={n} max={maxType} />
                </Link>
              ))}
            </div>
          </section>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.4fr) minmax(280px, 1fr)" }}>
        <section className="card">
          <header>
            <h2 className="grow">Latest events</h2>
            <Link to="/timeline" className="tiny">Open timeline →</Link>
          </header>
          <div className="list">
            {recent.isLoading && <Skeleton rows={6} />}
            {(recent.data?.items ?? []).map((e) => (
              <Link key={e.id} to={`/timeline/${e.id}`} className="item" style={{ color: "inherit" }}>
                <SevDot s={e.severity} />
                <span className="grow col" style={{ gap: 1 }}>
                  <span className="t trunc">{e.label}</span>
                  <span className="m">{e.camera_external_id} · {e.camera_name} · {stamp(e.started_at)}</span>
                </span>
                <span className="row" style={{ gap: 5 }}>
                  {e.plate_text && <span className="pill mono">{e.plate_text}</span>}
                  <Status s={e.status} />
                </span>
              </Link>
            ))}
            {recent.data?.items?.length === 0 && <Empty>Nothing on the timeline yet.</Empty>}
          </div>
        </section>

        <section className="card">
          <header><h2 className="grow">Busiest cameras</h2></header>
          <table className="tbl">
            <thead><tr><th>Camera</th><th>District</th><th style={{ textAlign: "right" }}>Events</th></tr></thead>
            <tbody>
              {(summary.data?.busiest_cameras ?? []).map((c: any) => (
                <tr key={c.external_id} onClick={() => nav(`/cameras/${c.external_id}`)} style={{ cursor: "pointer" }}>
                  <td><span className="mono">{c.external_id}</span> <span className="dim trunc">{c.name}</span></td>
                  <td className="dim">{c.district ?? "—"}</td>
                  <td className="n">{num(c.events)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {coverage?.from_ts && (
            <div className="tiny faint" style={{ padding: "8px 12px", borderTop: "1px solid var(--line-soft)" }}>
              Indexed footage spans {stamp(coverage.from_ts)} → {stamp(coverage.to_ts)} ({ago(coverage.to_ts)}).
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
