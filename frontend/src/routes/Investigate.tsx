import { lazy, Suspense, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  useGraph, useNextHop, useRebuildGraph, useStartDragnet, useTrack, useTrackReads,
} from "../lib/queries";
import { dur, fullStamp, hhmmss, num, pct, stamp } from "../lib/format";
import { Empty, ErrorBox, Pill, Skeleton, Spinner, Thumb } from "../components/ui";
import { LrBars, PlateVote, ScoreHeader } from "../components/Evidence";
import { useReport } from "../components/Toasts";

const CameraMap = lazy(() => import("../components/CameraMap"));
import { linksFromRefs } from "../components/CameraMap";

/**
 * Feature 9 — camera-to-camera investigation, driven one hop at a time.
 *
 * This is the manual counterpart to /reconstruct. The difference is who decides: the
 * reconstruction searches forward on its own and produces a route, this asks one question
 * about the cameras the operator picked and asserts nothing. Nothing here is persisted,
 * which is deliberate — an investigator trying a hypothesis should not be leaving a trail
 * of half-built pursuits behind them.
 *
 * The walk accumulates: confirming a candidate makes it the new anchor and pushes the old
 * one onto a breadcrumb, so the chain on screen is the chain of decisions the operator
 * made, each with the evidence that was on screen when they made it.
 */
export default function Investigate() {
  const [sp, setSp] = useSearchParams();
  const report = useReport();
  const graph = useGraph();
  const rebuild = useRebuildGraph();
  const hop = useNextHop();
  const dragnet = useStartDragnet();

  const [anchor, setAnchor] = useState(sp.get("track") ?? "");
  const [chain, setChain] = useState<{ track: string; camera: string; at: string; label: string }[]>([]);
  const [horizon, setHorizon] = useState(30);
  const [maxCameras, setMaxCameras] = useState(6);
  const [minRatio, setMinRatio] = useState(3);
  const [result, setResult] = useState<any>(null);

  const track = useTrack(anchor || undefined);
  const reads = useTrackReads(anchor || undefined);
  const t = track.data?.track;

  const nodes = graph.data?.nodes ?? [];
  const edges = graph.data?.edges ?? [];

  // Cameras the walk has touched, drawn as a route on the map.
  const walkCams = useMemo(() => {
    const ids = [...chain.map((c) => c.camera), t?.camera_external_id].filter(Boolean);
    return nodes.filter((n: any) => ids.includes(n.external_id));
  }, [chain, t, nodes]);
  const walkLinks = useMemo(() => {
    const seq = [...chain.map((c) => c.camera), t?.camera_external_id].filter(Boolean) as string[];
    return linksFromRefs(walkCams, seq.slice(1).map((dst, i) => ({ src: seq[i], dst, evidence: "confirmed" })));
  }, [chain, t, walkCams]);

  // Node ids are UUIDs and edges reference those; the map speaks external ids.
  const graphLinks = useMemo(() => {
    const byId = new Map(nodes.map((n: any) => [n.id, n.external_id]));
    return linksFromRefs(nodes, edges.map((e: any) => ({
      src: byId.get(e.src_camera_id), dst: byId.get(e.dst_camera_id), evidence: e.evidence,
    })));
  }, [nodes, edges]);

  const run = () => {
    if (!anchor) return;
    hop.mutate(
      { track_uuid: anchor, max_cameras: maxCameras, horizon_minutes: horizon, min_evidence_ratio: minRatio },
      {
        onSuccess: (d) => setResult(d),
        onError: (e: any) => report.err(e?.message ?? "Next hop failed"),
      },
    );
  };

  const step = (cand: any) => {
    if (t) {
      setChain((c) => [...c, {
        track: t.id, camera: t.camera_external_id, at: t.first_seen,
        label: `${t.plate_text ?? t.class ?? "subject"}`,
      }]);
    }
    setAnchor(cand.track_uuid);
    setSp({ track: cand.track_uuid }, { replace: true });
    setResult(null);
  };

  const reset = () => { setChain([]); setResult(null); };

  return (
    <div className="view col" style={{ gap: 14 }}>
      <section className="card">
        <header>
          <b>Camera-to-camera walk</b>
          <span className="grow" />
          <span className="tiny faint">
            {graph.data?.summary
              ? `${num(graph.data.summary.edges)} edges over ${num(graph.data.summary.cameras)} cameras — `
                + `${graph.data.summary.by_evidence?.plate ?? 0} from observed plates, `
                + `${graph.data.summary.by_evidence?.geo ?? 0} geographic, `
                + `${graph.data.summary.by_evidence?.confirmed ?? 0} operator-confirmed`
              : "graph not loaded"}
          </span>
          <button className="ghost sm" disabled={rebuild.isPending}
                  onClick={() => rebuild.mutate(undefined, {
                    onSuccess: (d: any) => report.ok(`Graph rebuilt — ${d.total_edges} edges`),
                    onError: (e: any) => report.err(e?.message ?? "Rebuild failed"),
                  })}>
            {rebuild.isPending ? "Rebuilding…" : "Rebuild graph"}
          </button>
        </header>
        <div className="body col" style={{ gap: 9 }}>
          <div className="row wrap" style={{ gap: 8 }}>
            <input value={anchor} onChange={(e) => setAnchor(e.target.value)} className="mono"
                   placeholder="Track UUID — pick one from search or a timeline event"
                   style={{ flex: 1, minWidth: 300 }} aria-label="Anchor track" />
            <label className="field">Horizon
              <select value={horizon} onChange={(e) => setHorizon(+e.target.value)}>
                {[10, 30, 60, 120].map((h) => <option key={h} value={h}>{h} min</option>)}
              </select>
            </label>
            <label className="field">Cameras
              <select value={maxCameras} onChange={(e) => setMaxCameras(+e.target.value)}>
                {[3, 6, 9, 12].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label className="field">Min evidence
              <select value={minRatio} onChange={(e) => setMinRatio(+e.target.value)}>
                {[1, 3, 8, 20].map((n) => <option key={n} value={n}>{n}×</option>)}
              </select>
            </label>
            <button className="primary" onClick={run} disabled={!anchor || hop.isPending}>
              {hop.isPending ? "Searching…" : "Where next?"}
            </button>
          </div>
          <div className="tiny faint">
            Searches the cameras reachable from the anchor inside the horizon and scores every
            vehicle that passed them in the expected arrival window. Nothing is saved — to keep a
            route, start a reconstruction instead.
          </div>
        </div>
      </section>

      {chain.length > 0 && (
        <div className="row wrap small" style={{ gap: 6 }}>
          <span className="faint">Walk:</span>
          {chain.map((c, i) => (
            <span key={c.track} className="row" style={{ gap: 6 }}>
              {i > 0 && <span className="faint">→</span>}
              <button className="pill" onClick={() => { setAnchor(c.track); setChain(chain.slice(0, i)); setResult(null); }}>
                {c.camera} <span className="faint">{hhmmss(c.at)}</span>
              </button>
            </span>
          ))}
          <span className="faint">→</span>
          <span className="pill accent">{t?.camera_external_id ?? "…"}</span>
          <button className="ghost sm" onClick={reset}>Reset walk</button>
        </div>
      )}

      {anchor && track.isError && <ErrorBox error={track.error} what="Anchor track" />}
      {anchor && track.isLoading && <Skeleton rows={4} />}

      <div className="split">
        <div className="l col" style={{ gap: 14 }}>
          {t && (
            <section className="card">
              <header><b>Subject</b><span className="grow" />
                <button className="sm" disabled={dragnet.isPending}
                        onClick={() => dragnet.mutate(
                          { track_uuid: t.id, title: `Walk from ${t.camera_external_id}`, horizon_minutes: 60, max_hops: 3 },
                          { onSuccess: () => report.ok("Reconstruction started — see Reconstruct"),
                            onError: (e: any) => report.err(e?.message ?? "Could not start") })}>
                  Hand to reconstruction
                </button>
              </header>
              <div className="body row" style={{ gap: 12, alignItems: "flex-start" }}>
                <div style={{ width: 150, flex: "0 0 150px" }}>
                  <Thumb src={t.thumbnail_url} label={t.class ?? "crop"} />
                </div>
                <dl className="kv grow">
                  <dt>Camera</dt>
                  <dd><Link to={`/cameras/${t.camera_external_id}`}>{t.camera_external_id}</Link> {t.camera_name}</dd>
                  <dt>Seen</dt><dd>{fullStamp(t.first_seen)}</dd>
                  <dt>For</dt><dd>{dur(t.duration_s)} over {t.detection_count} frames</dd>
                  <dt>Class</dt><dd>{t.dominant_color} {t.class}</dd>
                  <dt>Plate</dt>
                  <dd>{t.plate_text ?? "—"}{t.plate_confidence != null ? ` · ${pct(t.plate_confidence, 0)}` : ""}
                    {t.plate_read_count ? ` · ${t.plate_read_count} reads` : ""}</dd>
                  {t.heading_deg != null && <><dt>Heading</dt><dd>{t.heading_deg.toFixed(0)}°</dd></>}
                </dl>
              </div>
              {reads.data?.consensus?.votes?.positions && (
                <div className="body" style={{ paddingTop: 0 }}>
                  <div className="tiny faint" style={{ marginBottom: 6 }}>
                    Multi-frame plate consensus — agreement {pct(reads.data.agreement ?? 0, 0)} across{" "}
                    {reads.data.distinct_reads} distinct readings
                  </div>
                  <PlateVote votes={reads.data.consensus.votes} plate={reads.data.consensus.plate_text} />
                </div>
              )}
            </section>
          )}

          {hop.isPending && <div className="card"><div className="body"><Spinner label="Scoring candidates…" /></div></div>}

          {result && (
            <>
              <div className="row small">
                <span>
                  Examined {num(result.totals.examined)} vehicles across {num(result.totals.cameras)} cameras;
                  {" "}{num(result.totals.candidates)} cleared {minRatio}× evidence.
                </span>
              </div>
              {result.cameras.map((c: any) => (
                <section key={c.camera.id} className="card">
                  <header>
                    <Link to={`/cameras/${c.camera.external_id}`} className="mono">{c.camera.external_id}</Link>
                    <span className="trunc dim">{c.camera.name}</span>
                    <span className="grow" />
                    <Pill k={c.corridor.evidence === "confirmed" ? "ok" : c.corridor.evidence === "plate" ? "accent" : undefined}>
                      {c.corridor.evidence} corridor
                    </Pill>
                    <span className="tiny faint">
                      {c.examined} examined · {c.candidates.length} kept
                    </span>
                  </header>
                  <div className="body col" style={{ gap: 10 }}>
                    <div className="tiny faint">
                      Expected between {hhmmss(c.corridor.eta_from)} and {hhmmss(c.corridor.eta_to)}
                      {" "}({dur(c.corridor.median_transit_s)} typical, {c.corridor.transit_source}
                      {c.corridor.observations ? `, n=${c.corridor.observations}` : ", no observed transits"})
                      {" · corridor prior "}{pct(c.corridor.probability, 0)}
                    </div>

                    {c.examined === 0 && (
                      <Empty>
                        Nothing was indexed at this camera in that window, so there was nothing to
                        score. That is a gap in coverage, not an absence of the vehicle.
                      </Empty>
                    )}
                    {c.examined > 0 && c.candidates.length === 0 && (
                      <Empty>{c.examined} vehicles passed, none cleared {minRatio}× evidence.</Empty>
                    )}

                    {c.candidates.map((cand: any) => (
                      <div key={cand.track_uuid} className="item row" style={{ gap: 11, alignItems: "flex-start" }}>
                        <div style={{ width: 120, flex: "0 0 120px" }}>
                          <Thumb src={cand.thumbnail_url} label={cand.class ?? "crop"} />
                        </div>
                        <div className="grow col" style={{ gap: 7, minWidth: 0 }}>
                          <ScoreHeader candidate={cand} />
                          <div className="row wrap tiny faint" style={{ gap: 7 }}>
                            <span>{cand.dominant_color} {cand.class}</span>
                            {cand.plate_text && <span className="mono">{cand.plate_text} ({cand.plate_read_count} reads)</span>}
                            <span>{stamp(cand.first_seen)}</span>
                            <span>{dur(cand.duration_s)}</span>
                            {cand.visual_cos != null && <span>cos {cand.visual_cos.toFixed(3)}</span>}
                          </div>
                          <LrBars breakdown={cand.score_breakdown} />
                          <div className="row" style={{ gap: 6 }}>
                            <button className="primary sm" onClick={() => step(cand)}>
                              This one — continue from here
                            </button>
                            <button className="sm" onClick={() => { setAnchor(cand.track_uuid); setResult(null); }}>
                              Inspect
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </>
          )}

          {!anchor && (
            <Empty>
              Start from a track. Search for a vehicle, or open a timeline event that has one, and
              follow it here.
            </Empty>
          )}
        </div>

        <div className="r col" style={{ gap: 14 }}>
          <section className="card" style={{ height: 300 }}>
            <Suspense fallback={<Skeleton rows={4} />}>
              <CameraMap
                cameras={walkCams.length ? walkCams : nodes}
                links={walkLinks.length ? walkLinks : graphLinks}
                selected={t?.camera_external_id}
              />
            </Suspense>
          </section>

          <section className="card">
            <header><b>How a candidate is scored</b></header>
            <div className="body col small" style={{ gap: 7 }}>
              <p style={{ margin: 0 }}>
                Every candidate is judged on four independent lines of evidence, each expressed as
                a likelihood ratio — how many times more likely the observation is if this is the
                subject than if it is not. They multiply, and they are allowed to disagree.
              </p>
              <p style={{ margin: 0 }}>
                <b>Evidence</b> is the gate: it ignores how likely the vehicle was to be at that
                camera at all, so it is what decides whether a lead deserves your time.
                <b> Posterior</b> folds in the corridor prior and the size of the candidate pool,
                which makes it right for ranking and wrong for thresholding — a genuine match
                through a weak corridor at a busy camera scores low and is still the lead to follow.
              </p>
              <p style={{ margin: 0 }} className="faint">
                CLIP cosine runs 0.76–0.91 between completely different vehicles, so it is never used
                as an identity test — only calibrated against a same-class background sampled at that
                camera.
              </p>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
