import { lazy, Suspense, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useCreateEvidence, usePursuit, usePursuits, useRerunPursuit, useReviewSighting,
} from "../lib/queries";
import { ago, dur, fullStamp, hhmmss, num, pct } from "../lib/format";
import { Empty, ErrorBox, Modal, Pill, Skeleton, Thumb } from "../components/ui";
import { LrBars, ScoreHeader } from "../components/Evidence";
import { useReport } from "../components/Toasts";

const CameraMap = lazy(() => import("../components/CameraMap"));
import { linksFromRefs } from "../components/CameraMap";

/**
 * Cross-camera reconstruction — the route a vehicle took, assembled from cameras that
 * belong to different departments and never talk to each other.
 *
 * Two things on this screen are unusual enough to be worth stating plainly.
 *
 * First: nothing here is asserted. `confirmed` stays NULL until an operator reviews the
 * sighting, and the engine says so itself in `method.never_asserted`. The reconstruction
 * proposes; a person decides. Every sighting therefore renders with its full evidence
 * breakdown and two buttons, never as a fact.
 *
 * Second — and this is the part that makes the reconstruction falsifiable rather than
 * merely plausible — the engine writes down where it expects the vehicle to appear
 * *before* looking. Those are the `predictions`: a camera, an arrival window, a
 * probability, and a `resolved_by` that is filled in only if a sighting later landed
 * inside that window. That turns the route from a story into a claim that can be checked,
 * and it is the difference between "the model found a path" and "the model was right".
 */
export default function Reconstruct() {
  const { pursuitId } = useParams();
  const nav = useNavigate();
  const [status, setStatus] = useState("");
  const q = usePursuits({ status: status || undefined, limit: 40 });

  return (
    <div className="split">
      <div className="l col" style={{ gap: 10, padding: 14, overflow: "auto" }}>
        <div className="row" style={{ gap: 8 }}>
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="">All</option>
            {["active", "closed"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <span className="right small dim">{q.data?.count ?? 0} reconstructions</span>
        </div>

        {q.isError && <ErrorBox error={q.error} what="Reconstructions" />}
        {q.isLoading && <Skeleton rows={6} />}
        {(q.data?.items ?? []).length === 0 && !q.isLoading && (
          <Empty>None yet. Start one from a track in Investigate, or from an alert.</Empty>
        )}

        {(q.data?.items ?? []).map((p: any) => (
          <button key={p.id} className={`item col ${p.id === pursuitId ? "on" : ""}`}
                  style={{ gap: 5, textAlign: "left" }} onClick={() => nav(`/reconstruct/${p.id}`)}>
            <div className="row" style={{ gap: 6 }}>
              <span className="mono tiny">{p.ref}</span>
              <Pill k={p.status === "active" ? "open" : "closed"}>{p.status}</Pill>
              {p.unreviewed > 0 && <span className="right badge hot">{p.unreviewed} to review</span>}
            </div>
            <div className="trunc small">{p.title}</div>
            <div className="row wrap tiny faint" style={{ gap: 6 }}>
              {p.plate_text && <span className="mono">{p.plate_text}</span>}
              <span>{p.subject_color} {p.subject_class}</span>
              <span>from {p.seed_camera}</span>
            </div>
            <div className="row wrap tiny faint" style={{ gap: 6 }}>
              <span>{p.confirmed}/{p.candidates} confirmed</span>
              <span className="right">{ago(p.created_at)}</span>
            </div>
          </button>
        ))}
      </div>

      <div className="r" style={{ overflow: "auto" }}>
        {pursuitId ? <Detail id={pursuitId} /> : (
          <div className="body"><Empty>Pick a reconstruction to see its route and the evidence behind each hop.</Empty></div>
        )}
      </div>
    </div>
  );
}

function Detail({ id }: { id: string }) {
  const q = usePursuit(id);
  const report = useReport();
  const review = useReviewSighting();
  const rerun = useRerunPursuit();
  const [saving, setSaving] = useState(false);
  const [showAll, setShowAll] = useState(false);

  const p = q.data?.pursuit;
  const sightings: any[] = q.data?.sightings ?? [];
  const predictions: any[] = q.data?.predictions ?? [];
  const route: any[] = q.data?.route ?? [];
  const counts = q.data?.counts;
  const method = q.data?.method;

  const shown = showAll ? sightings : sightings.filter((s) => s.confirmed !== false);

  // The route as drawn: only hops a person has confirmed, plus the seed.
  const routeCams = useMemo(
    () => route.filter((r) => r.lat != null).map((r) => ({
      id: r.sighting_id, external_id: r.camera, name: r.camera_name,
      lat: r.lat, lng: r.lng, district: r.district, department: "police", open_event_count: 0,
    })) as any[],
    [route],
  );
  const routeLinks = useMemo(
    () => linksFromRefs(routeCams, routeCams.slice(1).map((c, i) => ({
      src: routeCams[i].external_id, dst: c.external_id, evidence: "confirmed",
    }))),
    [routeCams],
  );

  // The departmental systems this one route crosses — the Model 2 claim, counted from
  // the data rather than asserted.
  const systems = useMemo(() => {
    const m = new Map<string, number>();
    sightings.filter((s) => s.confirmed).forEach((s) => {
      const k = s.source_system ?? "unknown";
      m.set(k, (m.get(k) ?? 0) + 1);
    });
    return [...m.entries()];
  }, [sightings]);

  const resolved = predictions.filter((x) => x.resolved_by).length;
  const expired = predictions.filter((x) => x.expired && !x.resolved_by).length;

  if (q.isError) return <div className="body"><ErrorBox error={q.error} what="Reconstruction" /></div>;
  if (q.isLoading || !p) return <div className="body"><Skeleton rows={10} /></div>;

  const span = route.length > 1
    ? (new Date(route[route.length - 1].ts).getTime() - new Date(route[0].ts).getTime()) / 1000
    : 0;

  return (
    <div className="col" style={{ gap: 14, padding: 14 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <div>
          <div className="row" style={{ gap: 7 }}>
            <span className="mono dim">{p.ref}</span>
            <b style={{ fontSize: 15 }}>{p.title}</b>
            <Pill k={p.status === "active" ? "open" : "closed"}>{p.status}</Pill>
          </div>
          <div className="tiny faint">
            Seeded from <Link to={`/cameras/${p.seed_camera}`}>{p.seed_camera}</Link> at{" "}
            {fullStamp(p.seed_ts)} · {p.subject_color} {p.subject_class}
            {p.plate_text && <> · plate <span className="mono">{p.plate_text}</span></>}
            {p.created_by && ` · started by ${p.created_by}`}
          </div>
        </div>
        <span className="right row" style={{ gap: 6 }}>
          <button className="sm" disabled={!route.length} onClick={() => setSaving(true)}>Save as evidence</button>
          <button className="sm" disabled={rerun.isPending}
                  onClick={() => rerun.mutate(id, {
                    onSuccess: () => report.ok("Re-run against the current index"),
                    onError: (e: any) => report.err(e?.message ?? "Re-run failed"),
                  })}>
            {rerun.isPending ? "Re-running…" : "Re-run"}
          </button>
        </span>
      </div>

      <div className="grid g4">
        <div className="card stat"><div className="n">{num(counts?.confirmed ?? 0)}</div><div className="l">confirmed hops</div></div>
        <div className="card stat"><div className="n">{num(counts?.unreviewed ?? 0)}</div><div className="l">awaiting review</div></div>
        <div className="card stat cool"><div className="n">{systems.length}</div><div className="l">systems crossed</div></div>
        <div className="card stat"><div className="n">{span ? dur(span) : "—"}</div><div className="l">route span</div></div>
      </div>

      {systems.length > 0 && (
        <section className="card">
          <header><b>Departmental systems on this route</b></header>
          <div className="body col" style={{ gap: 5 }}>
            {systems.map(([name, n]) => (
              <div key={name} className="row small" style={{ gap: 7 }}>
                <span className="trunc grow">{name}</span>
                <span className="mono faint">{n} sighting{n === 1 ? "" : "s"}</span>
              </div>
            ))}
            <div className="tiny faint">
              The route was assembled across {systems.length} independent
              {systems.length === 1 ? " system" : " systems"} that hold their own footage and never
              exchange it. Only the metadata each one already produced was read.
            </div>
          </div>
        </section>
      )}

      {routeCams.length > 1 && (
        <section className="card" style={{ height: 280 }}>
          <Suspense fallback={<Skeleton rows={3} />}>
            <CameraMap cameras={routeCams} links={routeLinks} />
          </Suspense>
        </section>
      )}

      {route.length > 0 && (
        <section className="card">
          <header><b>Confirmed route</b><span className="grow" />
            <span className="tiny faint">seed plus every hop a person confirmed</span></header>
          <div className="body row wrap" style={{ gap: 7 }}>
            {route.map((r, i) => (
              <span key={r.sighting_id} className="row" style={{ gap: 7 }}>
                {i > 0 && (
                  <span className="tiny faint">
                    →{" "}{dur((new Date(r.ts).getTime() - new Date(route[i - 1].ts).getTime()) / 1000)}
                  </span>
                )}
                <span className={`hop ${r.match_kind === "seed" ? "" : "confirmed"}`}>
                  <b className="mono">{r.camera}</b>
                  <span className="tiny faint">{hhmmss(r.ts)}</span>
                  <span className="tiny">{r.match_kind}</span>
                </span>
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <header>
          <b>Predicted next appearances</b>
          <span className="grow" />
          <span className="tiny faint">
            {resolved} of {predictions.length} confirmed by a later sighting
            {expired > 0 && ` · ${expired} window passed with nothing`}
          </span>
        </header>
        <div className="body col" style={{ gap: 7 }}>
          <div className="tiny faint">
            Written before the search looked, so they can be wrong. A prediction that expired
            unresolved is a real result — either the vehicle turned off the corridor, or that
            camera had no coverage in the window.
          </div>
          {predictions.length === 0 && <Empty>No open prediction.</Empty>}
          {predictions
            .slice()
            .sort((a, b) => Number(!!b.resolved_by) - Number(!!a.resolved_by) || b.probability - a.probability)
            .map((x) => (
              <div key={x.id} className="item row" style={{ gap: 8 }}>
                <span className={`dot ${x.resolved_by ? "low" : x.expired ? "info" : "medium"}`} />
                <Link to={`/cameras/${x.camera}`} className="mono">{x.camera}</Link>
                <span className="trunc dim tiny grow">{x.camera_name}</span>
                <span className="tiny faint">
                  {hhmmss(x.eta_from)}–{hhmmss(x.eta_to)} · {x.hops} hop{x.hops === 1 ? "" : "s"}
                  {x.from_camera && ` via ${x.from_camera}`}
                </span>
                <span className="mono tiny">{pct(x.probability, 0)}</span>
                {x.resolved_by
                  ? <Pill k="ok">seen</Pill>
                  : x.expired ? <Pill>window passed</Pill> : <Pill k="pending">open</Pill>}
              </div>
            ))}
        </div>
      </section>

      <section className="card">
        <header>
          <b>Candidate sightings</b>
          <span className="grow" />
          <label className="check tiny">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            include rejected
          </label>
          <span className="tiny faint">{shown.length} shown</span>
        </header>
        <div className="body col" style={{ gap: 11 }}>
          {shown.length === 0 && <Empty>No candidate cleared the evidence gate.</Empty>}
          {shown.map((s) => (
            <div key={s.id} className="item row" style={{ gap: 11, alignItems: "flex-start" }}>
              <div className="col tiny" style={{ gap: 3, alignItems: "center", width: 120, flex: "0 0 120px" }}>
                <Thumb src={s.thumbnail_url} label={p.subject_class ?? "crop"} />
                <span className="badge">#{s.sequence_no}</span>
              </div>
              <div className="grow col" style={{ gap: 7, minWidth: 0 }}>
                <div className="row wrap" style={{ gap: 7 }}>
                  <Link to={`/cameras/${s.camera}`} className="mono">{s.camera}</Link>
                  <span className="trunc dim">{s.camera_name}</span>
                  <span className="tiny faint">{fullStamp(s.ts)}</span>
                  <span className="right">
                    {s.confirmed === true ? <Pill k="confirmed">confirmed</Pill>
                      : s.confirmed === false ? <Pill k="dismissed">rejected</Pill>
                      : <Pill k="pending">unreviewed</Pill>}
                  </span>
                </div>
                <div className="tiny faint">
                  {s.source_system ?? "unknown system"}
                  {s.reviewed_by && ` · reviewed by ${s.reviewed_by}`}
                </div>

                {s.match_kind === "seed" ? (
                  <div className="small faint">
                    The seed — this is the sighting the reconstruction started from, not a match.
                  </div>
                ) : (
                  <>
                    <ScoreHeader candidate={{
                      score: s.score,
                      evidence_ratio: s.score_breakdown?.evidence_ratio,
                      match_kind: s.match_kind,
                      hops: s.score_breakdown?.prior?.hops,
                      score_breakdown: s.score_breakdown,
                    }} />
                    <LrBars breakdown={s.score_breakdown} />
                  </>
                )}

                {s.match_kind !== "seed" && (
                  <div className="row" style={{ gap: 6 }}>
                    <button className="primary sm" disabled={review.isPending || s.confirmed === true}
                            onClick={() => review.mutate({ id: s.id, confirmed: true, pursuitId: id }, {
                              onSuccess: () => report.ok("Hop confirmed — it joins the route"),
                              onError: (e: any) => report.err(e?.message ?? "Review failed"),
                            })}>
                      Confirm
                    </button>
                    <button className="danger sm" disabled={review.isPending || s.confirmed === false}
                            onClick={() => review.mutate({ id: s.id, confirmed: false, pursuitId: id }, {
                              onSuccess: () => report.ok("Rejected — kept as a record of what the engine got wrong"),
                              onError: (e: any) => report.err(e?.message ?? "Review failed"),
                            })}>
                      Not the subject
                    </button>
                    {s.track_uuid && (
                      <Link className="sm" to={`/investigate?track=${s.track_uuid}`}>Walk from here</Link>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      {method && (
        <section className="card">
          <header><b>How to read this</b><span className="grow" />
            <span className="tiny faint">from the engine, not the UI</span></header>
          <div className="body col small" style={{ gap: 6 }}>
            <div><b>Framing.</b> {method.framing}</div>
            <div><b>Scores.</b> {method.reads}</div>
            <div><b>Appearance.</b> {method.visual_calibration}</div>
            <div><b>Nothing is asserted.</b> {method.never_asserted}</div>
            {p.params?.thresholds && (
              <div className="tiny faint">
                Run with: horizon {p.params.thresholds.horizon_minutes} min · max{" "}
                {p.params.thresholds.max_hops} hops · evidence gate{" "}
                {p.params.thresholds.min_evidence_ratio}× · posterior floor{" "}
                {p.params.thresholds.min_score}
              </div>
            )}
          </div>
        </section>
      )}

      {saving && (
        <SaveEvidence pursuitId={id} ref_={p.ref} route={route} onClose={() => setSaving(false)}
                      onDone={() => { report.ok("Route saved as evidence"); setSaving(false); }}
                      onFail={(e) => report.err(e)} />
      )}
    </div>
  );
}

function SaveEvidence({ pursuitId, ref_, route, onClose, onDone, onFail }: {
  pursuitId: string; ref_: string; route: any[];
  onClose: () => void; onDone: () => void; onFail: (e: string) => void;
}) {
  const [title, setTitle] = useState(`${ref_} — confirmed route`);
  const [note, setNote] = useState("");
  const m = useCreateEvidence();
  return (
    <Modal title="Save reconstruction as evidence" onClose={onClose}
           footer={<>
             <button className="ghost" onClick={onClose}>Cancel</button>
             <button className="primary" disabled={m.isPending}
                     onClick={() => m.mutate(
                       { title: title.trim(), note: note.trim(), pursuit_id: pursuitId },
                       { onSuccess: onDone, onError: (e: any) => onFail(e?.message ?? "Save failed") })}>
               {m.isPending ? "Saving…" : "Save"}
             </button>
           </>}>
      <label className="field">Title<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="field">Note
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)}
                  placeholder="What this route establishes." />
      </label>
      <p className="tiny faint">
        Captures the {route.length}-camera confirmed route and the evidence behind each hop as it
        stands now. Re-running the reconstruction later will not alter what was saved.
      </p>
    </Modal>
  );
}
