import { Suspense, lazy, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  useAlertSummary, useCameras, useEventSummary, useEvents, useFacets, useSystems,
} from "../lib/queries";
import { ago, num, stamp, titleCase } from "../lib/format";
import { Bar, Empty, ErrorBox, Help, Pill, SevDot, Skeleton, Status, Thumb } from "../components/ui";

const CameraMap = lazy(() => import("../components/CameraMap"));

/**
 * The wall view: where the estate is, what it has seen, and what nobody has
 * looked at yet.
 *
 * Laid out in three bands, in the order a shift actually reads them.
 *
 *   **The strip** is state — six figures that answer "is anything on fire".
 *   **The middle** is the estate itself. The map is the largest thing on the
 *   page because this is a geographic system: a camera is a *place*, and an
 *   event is a place and a time. Beside it runs the queue, which is the only
 *   part of the page that changes minute to minute.
 *   **The foot** is reference — what the console is federating, where the
 *   traffic is, which cameras can read a plate, and how far back the footage
 *   goes. True all shift, consulted rarely, so it sits below the fold.
 *
 * Every panel carries a "?". Nearly every number here is derived rather than
 * counted — "awaiting triage" is not "all events", ANPR capability is a measured
 * yield and not a spec sheet — and the derivation is what makes a figure worth
 * trusting. Putting it inline would bury the figure; leaving it out leaves an
 * operator guessing.
 *
 * Everything comes from aggregate endpoints rather than counting rows in the
 * browser: `/events/summary` is one query with three GROUP BYs against indexed
 * columns, replacing a tile-per-request fan-out.
 */
export default function Dashboard() {
  const nav = useNavigate();
  const summary = useEventSummary({});
  const alerts = useAlertSummary();
  const cameras = useCameras();
  const systems = useSystems();
  const facets = useFacets();
  const recent = useEvents({ limit: 14, order: "recent" });

  const cams = useMemo(() => cameras.data?.items ?? [], [cameras.data]);

  /** ANPR capability, measured per camera and stored on the row by
   *  scripts/score_camera_anpr.py — not a spec sheet figure. */
  const anpr = useMemo(() => {
    const tally = { anpr_capable: 0, occasional: 0, no_plates_observed: 0, overview_only: 0 };
    for (const c of cams) {
      const tier = (c.metadata as any)?.anpr?.tier as keyof typeof tally | undefined;
      if (tier && tier in tally) tally[tier] += 1;
    }
    return tally;
  }, [cams]);

  const estate = useMemo(() => ({
    total: cams.length,
    placed: cams.filter((c) => c.lat != null && c.lng != null).length,
    withEvents: cams.filter((c) => (c.open_event_count ?? 0) > 0).length,
    streaming: cams.filter((c) => c.has_stream).length,
  }), [cams]);

  if (summary.isError) return <div className="view"><ErrorBox error={summary.error} what="Dashboard" /></div>;

  const t = summary.data?.totals;
  const at = alerts.data?.totals;
  const byType: { event_type: string; n: number }[] = summary.data?.breakdown ?? [];
  const typeTotals = new Map<string, number>();
  for (const r of byType) typeTotals.set(r.event_type, (typeTotals.get(r.event_type) ?? 0) + Number(r.n));
  const types = [...typeTotals].sort((a, b) => b[1] - a[1]);
  const maxType = types[0]?.[1] ?? 1;
  const coverage = facets.data?.coverage;

  return (
    <div className="view dash">
      {/* ── State ─────────────────────────────────────────────────────────── */}
      <div className="kpis">
        <Kpi n={num(t?.open)} l="Awaiting triage" tone={t?.open ? "warm" : undefined}
             help="Events nobody has acknowledged, resolved or marked a false positive. This is the queue, not the total." />
        <Kpi n={num(at?.pending)} l="Alerts pending" tone={at?.pending ? "hot" : undefined}
             help="Watchlist hits waiting on a decision. An alert is one vehicle pass, not one frame — repeat reads of the same pass are collapsed." />
        <Kpi n={num(t?.critical)} l="Critical" tone={t?.critical ? "hot" : undefined}
             help="Events the rule that produced them rated critical. Severity is set by the detector, not by an operator." />
        <Kpi n={num(t?.total)} l="Events, all time"
             help="Everything the seven detector rules have produced over the indexed footage." />
        <Kpi n={`${estate.withEvents}/${estate.total}`} l="Cameras with open events" tone="cool"
             help="Cameras carrying at least one untriaged event. The rest are online and quiet." />
        <Kpi n={num(t?.false_positives)} l="False positives"
             help="Marked wrong by an operator. Kept rather than deleted — it is the only record of what a threshold got wrong, and what tuning one is based on." />
      </div>

      {/* ── The estate, and the queue against it ──────────────────────────── */}
      <div className="dash-main">
        <section className="card map-card">
          <header>
            <h2 className="grow">Estate</h2>
            <span className="map-legend">
              <i className="lg hot" /> open events
              <i className="lg ok" /> quiet
            </span>
            <Help>
              Every camera the registry knows about, at its surveyed position. Red
              means at least one event is waiting; click a pin to open that
              camera’s feed and its recent activity.
            </Help>
          </header>
          <div className="map-body">
            <Suspense fallback={<Skeleton rows={8} />}>
              {/* `fit` matters: the estate spans 20.8°–24.3°N and the default view
                  was Ahmedabad at zoom 11, so two thirds of the cameras sat off
                  screen and the map looked broken rather than zoomed. */}
              <CameraMap cameras={cams} fit onSelect={(ext) => nav(`/cameras/${ext}`)} />
            </Suspense>
          </div>
          <footer className="map-foot">
            <span><b>{estate.total}</b> cameras</span>
            <span><b>{estate.placed}</b> geocoded</span>
            <span><b>{estate.streaming}</b> with a live stream</span>
            <span className="grow" />
            <Link to="/cameras" className="tiny">Camera index →</Link>
          </footer>
        </section>

        <div className="dash-rail">
          <section className="card rail-feed">
            <header>
              <h2 className="grow">Latest events</h2>
              <Help>
                Newest first, across every camera. The crop is the frame the rule
                fired on — for a zone event the subject can be 40 px wide, so open
                the event for the scene diagram.
              </Help>
              <Link to="/timeline" className="tiny">Timeline →</Link>
            </header>
            <div className="list rail-list">
              {recent.isLoading && <Skeleton rows={6} />}
              {(recent.data?.items ?? []).map((e) => (
                <Link key={e.id} to={`/timeline/${e.id}`} className="item" style={{ color: "inherit" }}>
                  <SevDot s={e.severity} />
                  <Thumb src={e.thumbnail_url} alt={e.label} className="sm" zoomable={false}
                         label={titleCase(e.event_type)} />
                  <span className="grow col" style={{ gap: 1 }}>
                    <span className="t trunc">{e.label}</span>
                    <span className="m">{e.camera_external_id} · {stamp(e.started_at)}</span>
                  </span>
                  <span className="col" style={{ gap: 3, alignItems: "flex-end" }}>
                    <Status s={e.status} />
                    {e.plate_text && <span className="pill mono">{e.plate_text}</span>}
                  </span>
                </Link>
              ))}
              {recent.data?.items?.length === 0 && <Empty>Nothing on the timeline yet.</Empty>}
            </div>
          </section>

          <section className="card">
            <header>
              <h2 className="grow">Detector output</h2>
              <Help>
                How many events each of the seven rules has produced. A rule at
                zero has not fired on this footage — which is information about
                the footage, not a fault.
              </Help>
            </header>
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

      {/* ── Reference ─────────────────────────────────────────────────────── */}
      <div className="dash-foot">
        <section className="card">
          <header>
            <h2 className="grow">Connected systems</h2>
            <Pill k="accent">{systems.data?.count ?? "—"}</Pill>
            <Help>
              Distinct departmental camera systems this console is federating,
              counted from the estate rather than asserted. Each keeps its own
              identifiers and protocol.
            </Help>
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
          <header>
            <h2 className="grow">Can read a plate</h2>
            <Help>
              Measured per camera from what it actually delivered, not from a
              spec sheet. A plate needs roughly 90 px of width; most of this
              estate sits below that, which is optics rather than software.
            </Help>
          </header>
          <div className="body col" style={{ gap: 7 }}>
            <TierRow label="Reliably" n={anpr.anpr_capable} max={estate.total} tone="ok" />
            <TierRow label="Occasionally" n={anpr.occasional} max={estate.total} tone="warn" />
            <TierRow label="Overview only" n={anpr.overview_only} max={estate.total} />
            <TierRow label="No plates observed" n={anpr.no_plates_observed} max={estate.total} />
          </div>
        </section>

        <section className="card">
          <header>
            <h2 className="grow">Busiest cameras</h2>
            <Help>By event count over the indexed footage. Click a row to open it.</Help>
          </header>
          <table className="tbl">
            <thead><tr><th>Camera</th><th>District</th><th style={{ textAlign: "right" }}>Events</th></tr></thead>
            <tbody>
              {(summary.data?.busiest_cameras ?? []).slice(0, 6).map((c: any) => (
                <tr key={c.external_id} onClick={() => nav(`/cameras/${c.external_id}`)} style={{ cursor: "pointer" }}>
                  <td><span className="mono">{c.external_id}</span> <span className="dim trunc">{c.name}</span></td>
                  <td className="dim">{c.district ?? "—"}</td>
                  <td className="n">{num(c.events)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <header>
            <h2 className="grow">Indexed footage</h2>
            <Help>
              The window the pipeline has actually processed. A search outside it
              returns nothing for reasons that have nothing to do with the
              vehicle.
            </Help>
          </header>
          <div className="body col" style={{ gap: 8 }}>
            {coverage?.from_ts ? (
              <>
                <dl className="kv">
                  <dt>From</dt><dd>{stamp(coverage.from_ts)}</dd>
                  <dt>To</dt><dd>{stamp(coverage.to_ts)}</dd>
                  <dt>Latest</dt><dd>{ago(coverage.to_ts)}</dd>
                </dl>
                <p className="tiny faint" style={{ margin: 0 }}>
                  Live footage is relayed on demand; only this window is indexed and searchable.
                </p>
              </>
            ) : <Empty>No footage indexed yet.</Empty>}
          </div>
        </section>
      </div>
    </div>
  );
}

/** A figure, its label, and what it actually counts. */
function Kpi({ n, l, tone, help }:
  { n: React.ReactNode; l: string; tone?: "hot" | "warm" | "cool"; help: string }) {
  return (
    <div className={`card kpi ${tone ?? ""}`}>
      <div className="kpi-n">{n}</div>
      <div className="kpi-l">{l}<Help>{help}</Help></div>
    </div>
  );
}

function TierRow({ label, n, max, tone }:
  { label: string; n: number; max: number; tone?: "ok" | "warn" }) {
  return (
    <div className="col" style={{ gap: 3 }}>
      <div className="row small">
        <span className="grow">{label}</span>
        <span className="mono faint">{n}</span>
      </div>
      <Bar value={n} max={max || 1}
           color={tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--warn)" : undefined} />
    </div>
  );
}
