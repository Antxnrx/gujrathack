import { Suspense, lazy, useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  useAlertSummary, useCameras, useEventSummary, useEvents, useFacets,
  useLiveDetections, useSystems, useTracks,
} from "../lib/queries";
import { ago, hhmmss, num, stamp, titleCase } from "../lib/format";
import { Bar, Empty, ErrorBox, Help, Skeleton, Thumb } from "../components/ui";
import { Player } from "../components/Player";
import type { Camera, EventRow } from "../lib/types";

const CameraMap = lazy(() => import("../components/CameraMap"));

/**
 * The front door: cameras, and what they have just seen.
 *
 * What this replaced
 * ------------------
 * Six KPI cards, a map, a feed, a rule-by-rule breakdown and four reference
 * panels — eleven things competing for the first screen, none of which was a
 * camera. It read as a quarterly report for a surveillance estate rather than
 * the way into one, and the first question an operator actually has ("what is
 * happening, where") took a scroll and a click to reach.
 *
 * So the page is now two things. Cameras on the left, because this is a camera
 * system and the picture is the product. The log on the right, because the
 * second question is always "what just happened", and a log answers it in the
 * form people already read — one line, in time order, oldest meaning stale.
 *
 * Nothing was deleted. The estate map and the four reference panels are below
 * the fold, and the reference panels are folded shut: they are true all shift
 * and consulted about once a week, which is precisely what a disclosure is for.
 *
 * Tiles decode on request, not on load
 * ------------------------------------
 * Each tile starts as the most recent stored frame from its camera — a real
 * frame, labelled a still — and starts a decoder only when asked, at most
 * `LIVE_BUDGET` at a time. This is the same contract as the camera wall, on
 * purpose: one mental model for "still versus live" across the console, and the
 * landing page is the most-opened page in the app, so it is the last place that
 * should quietly start four 1080p decoders and a matching set of retry loops
 * against the departmental gateway.
 */

/** Live decoders permitted on this page. Lower than the wall's four: this is a
 *  page people leave open, and it is not the page for watching. */
const LIVE_BUDGET = 2;

/** Cameras shown on the front door. Enough to sweep, few enough to read. */
const TILES = 6;

export default function Dashboard() {
  const nav = useNavigate();
  const summary = useEventSummary({});
  const alerts = useAlertSummary();
  const cameras = useCameras();
  const systems = useSystems();
  const facets = useFacets();
  // 300, the endpoint's ceiling, because the log folds runs. One busy camera can
  // own every one of the last forty events — cam06 did, and the whole log
  // collapsed to a single line — so the fetch has to reach past the busiest
  // camera's run for the fold to leave anything else on screen.
  const recent = useEvents({ limit: 300, order: "recent" });
  const [liveIds, setLiveIds] = useState<string[]>([]);

  const cams = useMemo(() => cameras.data?.items ?? [], [cameras.data]);

  // Cameras with something waiting come first, then the busiest. A front door
  // that always shows the same six cameras in id order is a poster, not a view.
  const tiles = useMemo(
    () => [...cams]
      .sort((a, b) =>
        (b.open_event_count ?? 0) - (a.open_event_count ?? 0) ||
        (b.detection_count ?? 0) - (a.detection_count ?? 0))
      .slice(0, TILES),
    [cams],
  );

  const toggleLive = useCallback((id: string) => {
    setLiveIds((cur) => {
      if (cur.includes(id)) return cur.filter((x) => x !== id);
      const next = [...cur, id];
      // At the budget the oldest gives up its decoder rather than the request
      // being refused — asking for one more feed means you want it.
      return next.length > LIVE_BUDGET ? next.slice(next.length - LIVE_BUDGET) : next;
    });
  }, []);

  /**
   * Consecutive events of the same kind on the same camera, folded into one line.
   *
   * Not a cosmetic choice. cam06 watches a pedestrian crossing with a zone drawn
   * across the live carriageway, and it produced 87 zone intrusions in thirty
   * minutes — 87 distinct tracks with 87 distinct dedupe keys, so every one of
   * them is a real person and nothing is being double-counted. Printed one per
   * line they fill the log and bury every other camera in the estate, which is
   * how an operator ends up not reading the log at all.
   *
   * Folding a run keeps the count and the span, so nothing is hidden: the line
   * says twelve people crossed here over four minutes, which is the sentence
   * somebody would actually say. Events arrive newest-first, so the run's head
   * is its most recent member and `since` is the oldest seen so far.
   */
  const runs = useMemo(() => {
    const out: { head: EventRow; n: number; since: string }[] = [];
    for (const e of recent.data?.items ?? []) {
      const prev = out[out.length - 1];
      if (prev && prev.head.camera_external_id === e.camera_external_id
               && prev.head.event_type === e.event_type) {
        prev.n += 1;
        prev.since = e.started_at;
        continue;
      }
      out.push({ head: e, n: 1, since: e.started_at });
    }
    return out;
  }, [recent.data]);

  const estate = useMemo(() => ({
    total: cams.length,
    placed: cams.filter((c) => c.lat != null && c.lng != null).length,
    streaming: cams.filter((c) => c.has_stream).length,
    withEvents: cams.filter((c) => (c.open_event_count ?? 0) > 0).length,
  }), [cams]);

  /** ANPR capability, measured per camera by scripts/score_camera_anpr.py. */
  const anpr = useMemo(() => {
    const tally = { anpr_capable: 0, occasional: 0, no_plates_observed: 0, overview_only: 0 };
    for (const c of cams) {
      const tier = (c.metadata as any)?.anpr?.tier as keyof typeof tally | undefined;
      if (tier && tier in tally) tally[tier] += 1;
    }
    return tally;
  }, [cams]);

  if (summary.isError) return <div className="view"><ErrorBox error={summary.error} what="Dashboard" /></div>;

  const t = summary.data?.totals;
  const at = alerts.data?.totals;
  const coverage = facets.data?.coverage;

  return (
    <div className="dash2">
      {/* ── Three figures, on one line ──────────────────────────────────────
          Six cards became three numbers. A KPI card is a claim on attention,
          and five of the six were reference figures that had not changed all
          week — "events, all time" is not something anybody acts on. */}
      <div className="dash2-strip">
        <Stat n={num(t?.open)} l="awaiting triage" tone={t?.open ? "warn" : undefined}
              to="/timeline?status=new"
              help="Events nobody has acknowledged, resolved or marked a false positive. The queue, not the total." />
        <Stat n={num(at?.pending)} l="alerts pending" tone={at?.pending ? "hot" : undefined}
              to="/alerts"
              help="Watchlist hits waiting on a decision. One alert is one vehicle pass, not one frame." />
        <Stat n={num(t?.critical)} l="critical" tone={t?.critical ? "hot" : undefined}
              to="/timeline?severity=critical"
              help="Rated critical by the rule that produced them. Severity is set by the detector, not by an operator." />
        <span className="grow" />
        <span className="tiny faint">
          {estate.withEvents} of {estate.total} cameras carrying open events
        </span>
      </div>

      {/* ── Cameras, and the log ────────────────────────────────────────── */}
      <div className="dash2-main">
        <section className="dash2-cams">
          <header className="dash2-head">
            <b>Cameras</b>
            <Help>
              Busiest and most eventful first. Each tile is the latest stored
              frame until you start a decoder; at most {LIVE_BUDGET} run at once,
              because this page is left open. Open a camera for the live picture
              with detector boxes drawn on it.
            </Help>
            <span className="grow" />
            <span className="tiny faint">{liveIds.length}/{LIVE_BUDGET} live</span>
            <Link to="/wall" className="tiny">Wall →</Link>
            <Link to="/cameras" className="tiny">All {estate.total} →</Link>
          </header>
          <div className="dash2-grid">
            {cameras.isLoading && <Skeleton rows={6} />}
            {tiles.map((c) => (
              <DashTile key={c.id} cam={c}
                        live={liveIds.includes(c.external_id)}
                        onToggleLive={() => toggleLive(c.external_id)}
                        onOpen={() => nav(`/cameras/${c.external_id}`)} />
            ))}
          </div>
        </section>

        {/* One line per event, in time order. A log, not a feed of cards: the
            eye runs down a fixed time column and stops at the row that matters,
            which is the whole reason control rooms have always used one. */}
        <section className="dash2-log">
          <header className="dash2-head">
            <b>Log</b>
            <Help>
              Every event the detector rules have produced, newest first, across
              the estate. Click a line to open it with its evidence.
            </Help>
            <span className="grow" />
            <Link to="/timeline" className="tiny">Timeline →</Link>
          </header>
          <div className="logrows">
            {recent.isLoading && <Skeleton rows={10} />}
            {recent.data?.items?.length === 0 && <Empty>Nothing on the timeline yet.</Empty>}
            {runs.map(({ head: e, n, since }) => (
              <Link key={e.id} to={`/timeline/${e.id}`} className={`logrow sev-${e.severity}`}>
                <span className="logrow-t">{hhmmss(e.started_at)}</span>
                <span className="logrow-c mono">{e.camera_external_id}</span>
                <span className="logrow-k">{titleCase(e.event_type)}</span>
                <span className="logrow-m trunc">
                  {n > 1 ? `${n} since ${hhmmss(since)}` : e.label}
                </span>
                {n > 1 && <span className="logrow-n">×{n}</span>}
                {n === 1 && e.plate_text && <span className="logrow-p">{e.plate_text}</span>}
              </Link>
            ))}
          </div>
        </section>
      </div>

      {/* ── Below the fold ─────────────────────────────────────────────── */}
      <section className="card map-card dash2-map">
        <header>
          <h2 className="grow">Where the estate is</h2>
          <span className="map-legend"><i className="lg hot" /> open events<i className="lg ok" /> quiet</span>
          <Help>Every camera at its surveyed position. Click a pin to open it.</Help>
        </header>
        <div className="map-body">
          <Suspense fallback={<Skeleton rows={8} />}>
            <CameraMap cameras={cams} fit onSelect={(ext) => nav(`/cameras/${ext}`)} />
          </Suspense>
        </div>
        <footer className="map-foot">
          <span><b>{estate.total}</b> cameras</span>
          <span><b>{estate.placed}</b> geocoded</span>
          <span><b>{estate.streaming}</b> with a live stream</span>
        </footer>
      </section>

      {/* Folded shut. True all shift, read about once a week — which is what a
          disclosure is for, rather than four permanent panels. */}
      <details className="dash2-ref">
        <summary>Estate reference — systems, ANPR capability, busiest cameras, indexed window</summary>
        <div className="dash2-ref-grid">
          <section className="card">
            <header><h2 className="grow">Connected systems</h2>
              <Help>Departmental camera systems this console federates, counted from the estate rather than asserted.</Help>
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
                </div>
              ))}
            </div>
          </section>

          <section className="card">
            <header><h2 className="grow">Can read a plate</h2>
              <Help>
                Measured per camera from what it delivered, not a spec sheet. A plate
                needs roughly 90 px of width; most of this estate sits below that,
                which is optics rather than software.
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
            <header><h2 className="grow">Busiest cameras</h2>
              <Help>By event count over the indexed footage.</Help>
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
            <header><h2 className="grow">Indexed footage</h2>
              <Help>The window the pipeline has processed. A search outside it returns nothing for reasons unrelated to the vehicle.</Help>
            </header>
            <div className="body">
              {coverage?.from_ts ? (
                <dl className="kv">
                  <dt>From</dt><dd>{stamp(coverage.from_ts)}</dd>
                  <dt>To</dt><dd>{stamp(coverage.to_ts)}</dd>
                  <dt>Latest</dt><dd>{ago(coverage.to_ts)}</dd>
                </dl>
              ) : <Empty>No footage indexed yet.</Empty>}
            </div>
          </section>
        </div>
      </details>
    </div>
  );
}

/**
 * One camera on the front door.
 *
 * The overlay runs only on a tile that is actually decoding. Polling detections
 * for six stills would be six requests a second to annotate pictures that are
 * not moving.
 */
function DashTile({ cam, live, onToggleLive, onOpen }:
  { cam: Camera; live: boolean; onToggleLive: () => void; onOpen: () => void }) {
  const still = useTracks({ camera: cam.external_id, limit: 1 });
  const dets = useLiveDetections(cam.external_id, live);
  const shot = still.data?.items?.[0]?.thumbnail_url ?? null;

  return (
    <article className={`dtile${(cam.open_event_count ?? 0) > 0 ? " hot" : ""}`}>
      <header>
        <i className={`feed-dot ${cam.has_stream ? "on" : "off"}`}
           title={cam.has_stream ? "Live feed available" : "No feed configured"} />
        <span className="mono">{cam.external_id}</span>
        <span className="trunc dim">{cam.name}</span>
        <span className="grow" />
        {(cam.open_event_count ?? 0) > 0 && <span className="badge hot">{cam.open_event_count}</span>}
        <button className={live ? "primary sm" : "ghost sm"} onClick={onToggleLive}
                disabled={!cam.has_stream}
                title={live ? "Stop this decoder" : "Start a decoder for this tile"}>
          {live ? "live" : "still"}
        </button>
      </header>
      <div className="dtile-screen">
        {live ? (
          <Player cameraRef={cam.external_id} active compact
                  detections={dets.data?.items ?? []} />
        ) : (
          <button className="dtile-still" onClick={onOpen}
                  title="Most recent stored frame — press “still” for the live feed">
            <Thumb src={shot} zoomable={false}
                   label={still.isLoading ? "…" : "no frame stored"} />
            <span className="dtile-tag">still</span>
          </button>
        )}
      </div>
      <footer>
        <span>{num(cam.detection_count)} detections</span>
        <span>{num(cam.plate_count)} plates</span>
        <span className="grow" />
        <button className="ghost sm" onClick={onOpen}>Open →</button>
      </footer>
    </article>
  );
}

/** A figure on the top line. Each one links to the list it counts — a number
 *  you cannot click is a number you have to go and find. */
function Stat({ n, l, tone, help, to }:
  { n: React.ReactNode; l: string; tone?: "hot" | "warn"; help: string; to: string }) {
  return (
    <Link to={to} className={`dstat ${tone ?? ""}`}>
      <b>{n}</b>
      <span>{l}<Help>{help}</Help></span>
    </Link>
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
