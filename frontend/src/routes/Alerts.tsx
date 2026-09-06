import { useState } from "react";
import { Link } from "react-router-dom";
import {
  useAddWatch, useAlert, useAlertSummary, useAlerts, useRemoveWatch, useReviewAlert, useWatchlist,
} from "../lib/queries";
import { ago, fullStamp, hhmmss, num, pct, stamp, titleCase } from "../lib/format";
import { Empty, ErrorBox, Modal, Pager, Pill, Skeleton, Stat, Status, Thumb } from "../components/ui";
import { useReport } from "../components/Toasts";
import { StatusSelect } from "../components/Filters";

/**
 * Feature 8 — the live queue, and the watchlist that produces it.
 *
 * The queue and the watchlist share a page because they are one loop: an entry is added,
 * it raises hits, the hits get judged, and what the judging teaches goes back into the
 * entry's reason. Splitting them across two routes makes an operator navigate to do one
 * job.
 */
export default function Alerts() {
  const [offset, setOffset] = useState(0);
  const [statusF, setStatusF] = useState("pending");
  const [open, setOpen] = useState<string | null>(null);
  const list = useAlerts({ limit: 60, offset, status: statusF || undefined });
  const sum = useAlertSummary();
  const t = sum.data?.totals;

  return (
    <div className="view col" style={{ gap: 14 }}>
      <div className="grid g4">
        <Stat n={num(t?.total)} l="Alerts" />
        <Stat n={num(t?.pending)} l="Awaiting judgement" tone={t?.pending ? "hot" : undefined} />
        <Stat n={num(t?.confirmed)} l="Confirmed" />
        <Stat n={num(t?.dismissed)} l="Dismissed" />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.5fr) minmax(300px, 1fr)" }}>
        <section className="card">
          <header>
            <h2 className="grow">Queue</h2>
            <StatusSelect value={statusF} onChange={(v) => { setStatusF(v); setOffset(0); }} options={[
              ["pending", "Pending"], ["confirmed", "Confirmed"], ["dismissed", "Dismissed"], ["", "All"],
            ]} />
          </header>
          {list.isError && <div style={{ padding: 12 }}><ErrorBox error={list.error} what="Alerts" /></div>}
          {list.isLoading && <Skeleton rows={6} />}
          <div className={`list ${list.isFetching && !list.isLoading ? "stale" : ""}`}>
            {(list.data?.items ?? []).map((a) => (
              <button key={a.id} className="item" onClick={() => setOpen(a.id)}>
                <span className="th"><Thumb src={a.thumbnail_url} label="no crop" /></span>
                <span className="grow col" style={{ gap: 2 }}>
                  <span className="t row" style={{ gap: 6 }}>
                    <b className="mono">{a.plate_text ?? a.watchlist_value ?? titleCase(a.alert_type)}</b>
                    <Pill k={a.alert_type === "watchlist_plate" ? "danger" : "warn"}>{titleCase(a.alert_type)}</Pill>
                  </span>
                  <span className="m">
                    {hhmmss(a.ts)} · {a.camera_external_id} {a.camera_name} · {titleCase(a.class)} {titleCase(a.dominant_color)}
                  </span>
                  {a.watchlist_reason && <span className="m trunc">“{a.watchlist_reason}”</span>}
                </span>
                <span className="col" style={{ gap: 3, alignItems: "flex-end" }}>
                  <Status s={a.status} />
                  {/* The consensus read and the single-frame read side by side, because
                      they disagree often and only one of them is trustworthy. */}
                  {a.consensus_plate && a.consensus_plate !== a.plate_text && (
                    <span className="tiny faint mono" title="Track consensus">≈{a.consensus_plate}</span>
                  )}
                  {a.plate_read_count ? <span className="tiny faint">{a.plate_read_count} reads</span> : null}
                </span>
              </button>
            ))}
            {list.data?.items?.length === 0 && !list.isLoading && (
              <Empty>Nothing {statusF || "at all"} in the queue.</Empty>
            )}
          </div>
          <Pager page={list.data} onPage={setOffset} />
        </section>

        <WatchlistPanel />
      </div>

      {open && <AlertModal id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function AlertModal({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useAlert(id);
  const review = useReviewAlert();
  const rep = useReport();
  const [note, setNote] = useState("");

  const a = q.data?.alert ?? q.data;
  const reads = q.data?.plate_reads ?? [];
  const notes = q.data?.notes ?? [];

  const judge = (status: string) =>
    review.mutate({ id, status, note: note.trim() || undefined }, {
      onSuccess: () => { rep.ok(`Alert ${status}`); onClose(); },
      onError: (e) => rep.err(e, "Review"),
    });

  return (
    <Modal title={`Alert · ${a?.plate_text ?? a?.watchlist_value ?? ""}`} onClose={onClose} footer={
      <>
        <button className="ghost" onClick={onClose}>Close</button>
        <button className="danger" disabled={review.isPending} onClick={() => judge("dismissed")}>Not the vehicle</button>
        <button className="primary" disabled={review.isPending} onClick={() => judge("confirmed")}>Confirm hit</button>
      </>
    }>
      {q.isLoading && <Skeleton rows={5} />}
      {q.isError && <ErrorBox error={q.error} what="Alert" />}
      {a && (
        <>
          <Thumb src={a.thumbnail_url} label="no crop stored — MinIO bucket is empty" />
          <dl className="kv">
            <dt>Matched</dt><dd className="mono">{a.watchlist_value ?? "—"} ({a.watchlist_type})</dd>
            <dt>Read as</dt><dd className="mono">{a.plate_text ?? "—"} {a.plate_confidence != null ? `· ${pct(a.plate_confidence, 0)}` : ""}</dd>
            <dt>Track consensus</dt>
            <dd className="mono">
              {a.consensus_plate ?? "—"}
              {a.consensus_confidence != null ? ` · ${pct(a.consensus_confidence, 0)}` : ""}
              {a.plate_read_count ? ` · ${a.plate_read_count} reads` : ""}
            </dd>
            <dt>Vehicle</dt><dd>{titleCase(a.class)} · {titleCase(a.dominant_color)}</dd>
            <dt>Camera</dt><dd><Link to={`/cameras/${a.camera_external_id}`}>{a.camera_external_id}</Link> {a.camera_name}</dd>
            <dt>When</dt><dd>{fullStamp(a.ts)} <span className="faint">({ago(a.ts)})</span></dd>
            <dt>Reason on file</dt><dd style={{ fontFamily: "inherit" }}>{a.watchlist_reason ?? "—"}</dd>
            {a.event_id && <><dt>Timeline</dt><dd><Link to={`/timeline/${a.event_id}`}>open mirrored event →</Link></dd></>}
          </dl>

          {reads.length > 0 && (
            <section className="card">
              <header><h4 className="grow">Every read on this track</h4><span className="badge">{reads.length}</span></header>
              <div className="body">
                {/* This table is the argument for track-level ANPR. On this footage a
                    single frame is exactly right about 1 time in 6; the vote across the
                    track is what the alert actually fired on. */}
                <table className="tbl">
                  <thead><tr><th>Frame</th><th>Read</th><th style={{ textAlign: "right" }}>Conf</th></tr></thead>
                  <tbody>
                    {reads.map((r: any, i: number) => (
                      <tr key={r.id ?? i}>
                        <td className="faint">{hhmmss(r.ts)}</td>
                        <td className="mono" style={{ color: r.plate_text === a.consensus_plate ? "var(--ok)" : undefined }}>
                          {r.plate_text ?? "—"}
                        </td>
                        <td className="n">{pct(r.plate_confidence, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {notes.length > 0 && (
            <section className="card">
              <header><h4 className="grow">Notes</h4></header>
              <div className="body col">
                {notes.map((n: any) => (
                  <div key={n.id} className="col" style={{ gap: 2 }}>
                    <div className="row tiny faint">
                      <b>{n.operator}</b><span>{stamp(n.created_at)}</span>
                      {n.target_type === "event" && <Pill k="accent">from timeline</Pill>}
                    </div>
                    <div style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <label className="field">Why (saved with the decision)
            <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)}
                      placeholder="Confirmed against the reason on file / plate read is wrong / different vehicle" />
          </label>
        </>
      )}
    </Modal>
  );
}

function WatchlistPanel() {
  const q = useWatchlist();
  const add = useAddWatch();
  const remove = useRemoveWatch();
  const rep = useReport();
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [backfill, setBackfill] = useState(true);

  const entries = q.data?.entries ?? [];

  return (
    <section className="card">
      <header><h2 className="grow">Watchlist</h2><span className="badge">{entries.length}</span></header>
      <div className="body col">
        <form className="col" style={{ gap: 6 }} onSubmit={(e) => {
          e.preventDefault();
          const v = value.toUpperCase().replace(/[^A-Z0-9]/g, "");
          if (!v || !reason.trim()) return;
          add.mutate({ type: "plate", value: v, reason: reason.trim(), backfill }, {
            onSuccess: (r: any) => {
              rep.ok(backfill ? `Added — ${r?.backfilled ?? r?.alerts ?? 0} historical hits raised` : "Added to watchlist");
              setValue(""); setReason("");
            },
            onError: (x) => rep.err(x, "Watchlist"),
          });
        }}>
          <input className="mono" placeholder="GJ01AB1234" value={value}
                 onChange={(e) => setValue(e.target.value.toUpperCase())} aria-label="Plate" />
          <input placeholder="Why this plate is wanted" value={reason}
                 onChange={(e) => setReason(e.target.value)} aria-label="Reason" />
          {/* Opt-in, and worth being explicit about: with this on, adding an entry
              re-reads indexed history and can raise a queue of hits in one statement. */}
          <label className="check">
            <input type="checkbox" checked={backfill} onChange={(e) => setBackfill(e.target.checked)} />
            Also search footage already indexed
          </label>
          <button className="primary" disabled={add.isPending || !value || !reason.trim()}>Add to watchlist</button>
        </form>

        <hr className="sep" />

        {q.isLoading && <Skeleton rows={3} />}
        {entries.length === 0 && !q.isLoading && <Empty>No entries.</Empty>}
        {entries.map((w: any) => (
          <div key={w.id} className="row small" style={{ alignItems: "flex-start" }}>
            <span className="col grow" style={{ gap: 1 }}>
              <span className="mono">{w.value ?? titleCase(w.type)}</span>
              <span className="tiny faint">{w.reason}</span>
              <span className="tiny faint">
                {w.alert_count != null ? `${w.alert_count} hits` : ""}
                {w.pending_alerts ? ` · ${w.pending_alerts} pending` : ""}
                {` · added ${ago(w.created_at)}`}
              </span>
            </span>
            <button className="ghost sm" title="Remove"
                    onClick={() => remove.mutate(w.id, {
                      onSuccess: () => rep.ok("Removed — its alerts are kept and detached"),
                      onError: (x) => rep.err(x),
                    })}>×</button>
          </div>
        ))}
      </div>
    </section>
  );
}
