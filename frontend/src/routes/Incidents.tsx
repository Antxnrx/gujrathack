import { lazy, Suspense, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAddNote, useDeleteNote, useIncident, useIncidents, useRemoveIncidentEvent,
  useReorderIncident, useUpdateIncident,
} from "../lib/queries";
import { ago, dur, fullStamp, hhmmss, num, stamp, titleCase } from "../lib/format";
import { Empty, ErrorBox, Modal, Pager, Pill, SevDot, Skeleton, Status, Thumb } from "../components/ui";
import { useReport } from "../components/Toasts";

const CameraMap = lazy(() => import("../components/CameraMap"));
import { linksFromRefs } from "../components/CameraMap";

/**
 * Feature 3 — incident timelines.
 *
 * Two things share this file because they are one screen at two zoom levels: the case
 * list, and one case opened. `/incidents/:id` renders the detail beside the list rather
 * than replacing it, so an investigator comparing two cases does not lose their place.
 *
 * The ordering is explicit and stored, not derived from `started_at`. That is the whole
 * point of a case file: the investigator's account of what happened is a claim about
 * sequence that may disagree with the clock — a camera whose NTP drifted, a sighting
 * entered late — and the timeline has to be able to carry the human's version.
 */
export default function Incidents() {
  const { incidentId } = useParams();
  const nav = useNavigate();
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [offset, setOffset] = useState(0);

  const q = useIncidents({
    status: status || undefined, priority: priority || undefined, limit: 30, offset,
  });
  const items = q.data?.items ?? [];

  return (
    <div className="split">
      <div className="l col" style={{ gap: 10, padding: 14, overflow: "auto" }}>
        <div className="row wrap" style={{ gap: 8 }}>
          <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}
                  aria-label="Status">
            <option value="">All statuses</option>
            {["open", "investigating", "closed"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
          <select value={priority} onChange={(e) => { setPriority(e.target.value); setOffset(0); }}
                  aria-label="Priority">
            <option value="">Any priority</option>
            {["critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
          <span className="right small dim">{q.data?.count ?? 0} cases</span>
        </div>

        {q.isError && <ErrorBox error={q.error} what="Incidents" />}
        {q.isLoading && <Skeleton rows={6} />}
        {items.length === 0 && !q.isLoading && (
          <Empty>No case yet. Promote an event from the timeline to open one.</Empty>
        )}

        {items.map((i: any) => (
          <button key={i.id} className={`item col ${i.id === incidentId ? "on" : ""}`}
                  style={{ gap: 5, textAlign: "left" }} onClick={() => nav(`/incidents/${i.id}`)}>
            <div className="row" style={{ gap: 6 }}>
              <span className="mono tiny">{i.ref}</span>
              <Pill k={i.priority}>{i.priority}</Pill>
              <span className="right"><Status s={i.status} /></span>
            </div>
            <div className="trunc" style={{ fontSize: 13 }}>{i.title}</div>
            <div className="row wrap tiny faint" style={{ gap: 6 }}>
              <span>{num(i.event_count)} events</span>
              <span>{num(i.camera_count)} cameras</span>
              {i.note_count > 0 && <span>{num(i.note_count)} notes</span>}
              {i.snapshot_count > 0 && <span>{num(i.snapshot_count)} snapshots</span>}
              <span className="right">{ago(i.created_at)}</span>
            </div>
            {i.first_event_at && (
              <div className="tiny faint">
                {stamp(i.first_event_at)} → {stamp(i.last_event_at)}
                {" · spans "}
                {dur((new Date(i.last_event_at).getTime() - new Date(i.first_event_at).getTime()) / 1000)}
              </div>
            )}
            {(i.severities ?? []).length > 0 && (
              <div className="row" style={{ gap: 3 }}>
                {i.severities.map((s: string) => <SevDot key={s} s={s} />)}
              </div>
            )}
          </button>
        ))}
        <Pager page={q.data} onPage={setOffset} />
      </div>

      <div className="r" style={{ overflow: "auto" }}>
        {incidentId ? <Detail id={incidentId} /> : (
          <div className="body"><Empty>Pick a case to see its timeline.</Empty></div>
        )}
      </div>
    </div>
  );
}

function Detail({ id }: { id: string }) {
  const q = useIncident(id);
  const report = useReport();
  const upd = useUpdateIncident();
  const rm = useRemoveIncidentEvent();
  const reorder = useReorderIncident();
  const addNote = useAddNote();
  const delNote = useDeleteNote();
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);

  const inc = q.data?.incident;
  const timeline: any[] = q.data?.timeline ?? [];
  const path: any[] = q.data?.camera_path ?? [];
  const notes: any[] = q.data?.notes ?? [];
  const snaps: any[] = q.data?.snapshots ?? [];
  const pursuits: any[] = q.data?.pursuits ?? [];

  const cams = useMemo(
    () => path.filter((p) => p.lat != null).map((p) => ({
      id: p.camera_id, external_id: p.camera_external_id, name: p.camera_name,
      lat: p.lat, lng: p.lng, district: p.district, open_event_count: p.events,
      department: "police",
    })) as any[],
    [path],
  );
  // The route the case describes, as consecutive pairs of the ordered camera path.
  const links = useMemo(
    () => linksFromRefs(cams, cams.slice(1).map((c, i) => ({
      src: cams[i].external_id, dst: c.external_id, evidence: "confirmed",
    }))),
    [cams],
  );

  const move = (idx: number, delta: number) => {
    const next = [...timeline];
    const j = idx + delta;
    if (j < 0 || j >= next.length) return;
    [next[idx], next[j]] = [next[j], next[idx]];
    reorder.mutate({ id, event_ids: next.map((e) => e.id) },
      { onError: (e: any) => report.err(e?.message ?? "Reorder failed") });
  };

  if (q.isError) return <div className="body"><ErrorBox error={q.error} what="Incident" /></div>;
  if (q.isLoading || !inc) return <div className="body"><Skeleton rows={10} /></div>;

  return (
    <div className="col" style={{ gap: 14, padding: 14 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <div>
          <div className="row" style={{ gap: 7 }}>
            <span className="mono dim">{inc.ref}</span>
            <b style={{ fontSize: 15 }}>{inc.title}</b>
          </div>
          <div className="tiny faint">
            Opened {fullStamp(inc.created_at)} by {inc.created_by ?? "unknown"}
            {inc.closed_at && ` · closed ${stamp(inc.closed_at)}`}
          </div>
        </div>
        <span className="right row" style={{ gap: 6 }}>
          <select className="sm" value={inc.status} aria-label="Status"
                  onChange={(e) => upd.mutate({ id, status: e.target.value },
                    { onSuccess: () => report.ok(`Case ${e.target.value}`),
                      onError: (x: any) => report.err(x?.message ?? "Update failed") })}>
            {["open", "investigating", "closed"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
          <select className="sm" value={inc.priority} aria-label="Priority"
                  onChange={(e) => upd.mutate({ id, priority: e.target.value },
                    { onError: (x: any) => report.err(x?.message ?? "Update failed") })}>
            {["low", "medium", "high", "critical"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
          <button className="ghost sm" onClick={() => setEditing(true)}>Edit</button>
        </span>
      </div>

      {inc.description && <p className="small dim" style={{ margin: 0 }}>{inc.description}</p>}

      {cams.length > 1 && (
        <section className="card" style={{ height: 240 }}>
          <Suspense fallback={<Skeleton rows={3} />}>
            <CameraMap cameras={cams} links={links} />
          </Suspense>
        </section>
      )}

      {path.length > 0 && (
        <section className="card">
          <header><b>Camera path</b><span className="grow" />
            <span className="tiny faint">in the order the case asserts</span></header>
          <div className="body row wrap" style={{ gap: 6 }}>
            {path.map((p, i) => (
              <span key={p.camera_id} className="row" style={{ gap: 6 }}>
                {i > 0 && <span className="faint">→</span>}
                <Link to={`/cameras/${p.camera_external_id}`} className="pill accent">
                  {p.camera_external_id}
                  <span className="faint"> {hhmmss(p.from)}</span>
                  {p.events > 1 && <span className="faint"> ·{p.events}</span>}
                </Link>
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <header><b>Timeline</b><span className="grow" />
          <span className="tiny faint">{timeline.length} events</span></header>
        <div className="body col" style={{ gap: 8 }}>
          {timeline.length === 0 && <Empty>No event attached. Add one from the timeline view.</Empty>}
          {timeline.map((e, i) => (
            <div key={e.id} className="item row" style={{ gap: 9, alignItems: "flex-start" }}>
              <div className="col tiny" style={{ gap: 2, alignItems: "center", width: 26 }}>
                <span className="badge">{e.sequence_no ?? i + 1}</span>
                <button className="ghost sm" title="Earlier in the account"
                        onClick={() => move(i, -1)} disabled={i === 0 || reorder.isPending}>↑</button>
                <button className="ghost sm" title="Later in the account"
                        onClick={() => move(i, 1)} disabled={i === timeline.length - 1 || reorder.isPending}>↓</button>
              </div>
              <Thumb src={e.thumbnail_url} label={e.event_type} className="sm" />
              <div className="grow col" style={{ gap: 3, minWidth: 0 }}>
                <div className="row" style={{ gap: 6 }}>
                  <SevDot s={e.severity} />
                  <Link to={`/timeline/${e.id}`} className="trunc">{e.label}</Link>
                  <span className="right"><Status s={e.status} /></span>
                </div>
                <div className="row wrap tiny faint" style={{ gap: 6 }}>
                  <span className="mono">{e.camera_external_id}</span>
                  <span>{fullStamp(e.started_at)}</span>
                  {e.plate_text && <span className="mono">{e.plate_text}</span>}
                  <span>{e.event_type}</span>
                  {e.source !== "manual" && <span>{e.source}</span>}
                </div>
                {e.link_note && <div className="tiny">{e.link_note}</div>}
                <div className="tiny faint">
                  added {ago(e.added_at)}{e.added_by ? ` by ${e.added_by}` : ""}
                </div>
              </div>
              <button className="ghost sm" title="Detach from this case"
                      onClick={() => rm.mutate({ id, eventId: e.id },
                        { onSuccess: () => report.ok("Detached from case"),
                          onError: (x: any) => report.err(x?.message ?? "Detach failed") })}>×</button>
            </div>
          ))}
        </div>
      </section>

      {pursuits.length > 0 && (
        <section className="card">
          <header><b>Reconstructions</b></header>
          <div className="body col" style={{ gap: 6 }}>
            {pursuits.map((p: any) => (
              <Link key={p.id} to={`/reconstruct/${p.id}`} className="item row" style={{ gap: 7 }}>
                <span className="trunc grow">{p.title ?? p.seed_plate ?? "reconstruction"}</span>
                <Pill>{p.status}</Pill>
                <span className="tiny faint">{ago(p.created_at)}</span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {snaps.length > 0 && (
        <section className="card">
          <header><b>Evidence snapshots</b></header>
          <div className="body col" style={{ gap: 6 }}>
            {snaps.map((s: any) => (
              <Link key={s.id} to="/evidence" className="item row" style={{ gap: 7 }}>
                <span className="trunc grow">{s.title}</span>
                <span className="tiny faint">{ago(s.created_at)}</span>
              </Link>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <header><b>Case notes</b></header>
        <div className="body col" style={{ gap: 8 }}>
          {notes.map((n: any) => (
            <div key={n.id} className="item col" style={{ gap: 3 }}>
              <div className="row tiny faint">
                <b>{n.operator}</b>
                <span className="right">{fullStamp(n.created_at)}</span>
                <button className="ghost sm" onClick={() => delNote.mutate(n.id)}>×</button>
              </div>
              <div className="small" style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
            </div>
          ))}
          <div className="row" style={{ gap: 6 }}>
            <input value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder="What you concluded, and from what."
                   onKeyDown={(e) => { if (e.key === "Enter" && note.trim()) submitNote(); }} />
            <button className="primary" disabled={!note.trim() || addNote.isPending} onClick={submitNote}>Add</button>
          </div>
        </div>
      </section>

      {editing && (
        <EditModal inc={inc} onClose={() => setEditing(false)}
                   onSave={(b) => upd.mutate({ id, ...b }, {
                     onSuccess: () => { report.ok("Case updated"); setEditing(false); },
                     onError: (x: any) => report.err(x?.message ?? "Update failed"),
                   })} />
      )}
    </div>
  );

  function submitNote() {
    addNote.mutate(
      { target_type: "incident", target_id: id, body: note.trim() },
      { onSuccess: () => setNote(""), onError: (x: any) => report.err(x?.message ?? "Note failed") },
    );
  }
}

function EditModal({ inc, onClose, onSave }: {
  inc: any; onClose: () => void; onSave: (b: Record<string, unknown>) => void;
}) {
  const [title, setTitle] = useState(inc.title);
  const [description, setDescription] = useState(inc.description ?? "");
  return (
    <Modal title={`Edit ${inc.ref}`} onClose={onClose}
           footer={<>
             <button className="ghost" onClick={onClose}>Cancel</button>
             <button className="primary" disabled={!title.trim()}
                     onClick={() => onSave({ title: title.trim(), description })}>Save</button>
           </>}>
      <label className="field">Title<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="field">Description
        <textarea rows={5} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
    </Modal>
  );
}
