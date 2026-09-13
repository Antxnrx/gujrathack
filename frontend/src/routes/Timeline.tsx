import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  useAddNote, useApplyTags, useEvent, useEvents, useIncidents, useAddIncidentEvents,
  useRemoveTag, useToggleBookmark, useTriage,
} from "../lib/queries";
import { ago, dur, fullStamp, hhmmss, num, pct, stamp, titleCase } from "../lib/format";
import { CameraSelect, EventTypeSelect, StatusSelect, TagSelect, useDebounced } from "../components/Filters";
import { VirtualList } from "../components/VirtualList";
import {
  Empty, ErrorBox, Modal, Pager, Pill, Plate, SceneDiagram, SevDot, Skeleton, Status, Thumb,
} from "../components/ui";
import { useReport } from "../components/Toasts";
import type { EventRow } from "../lib/types";

/**
 * Features 1, 3, 6, 7 and 8 in one surface: the timeline, tagging, triage, bookmarks,
 * notes, and promotion into an incident.
 *
 * It is a master/detail rather than a table with a modal, because triage is a loop —
 * read, decide, next — and a modal makes the operator close something between every
 * decision. Arrow keys move the selection so the loop can be run without the mouse.
 */
export default function Timeline() {
  const { eventId } = useParams();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const rep = useReport();

  const [offset, setOffset] = useState(0);
  const [plateRaw, setPlateRaw] = useState("");
  const plate = useDebounced(plateRaw.toUpperCase().replace(/[^A-Z0-9]/g, ""));

  const eventType = sp.get("event_type") ?? "";
  const statusF = sp.get("status") ?? "";
  const camera = sp.get("camera") ?? "";
  const tag = sp.get("tag") ?? "";
  const order = sp.get("order") ?? "recent";
  const setF = (k: string, v: string) => {
    const next = new URLSearchParams(sp);
    v ? next.set(k, v) : next.delete(k);
    setSp(next, { replace: true });
    setOffset(0);
  };

  const params = useMemo(() => ({
    limit: 100, offset, order,
    event_type: eventType || undefined,
    status: statusF || undefined,
    camera: camera || undefined,
    tag: tag || undefined,
    plate: plate || undefined,
  }), [offset, order, eventType, statusF, camera, tag, plate]);

  const list = useEvents(params);
  const items = list.data?.items ?? [];

  const move = (delta: number) => {
    const i = items.findIndex((e) => e.id === eventId);
    const next = items[Math.max(0, Math.min(items.length - 1, (i < 0 ? 0 : i) + delta))];
    if (next) nav(`/timeline/${next.id}`, { replace: true });
  };

  return (
    <div className="split l" style={{ height: "100%" }}>
      <div className="pane-l col" style={{ gap: 0 }}>
        <div className="row wrap" style={{ gap: 6, padding: 10, borderBottom: "1px solid var(--line)" }}
             onKeyDown={(e) => {
               if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
               if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
             }}>
          <EventTypeSelect value={eventType} onChange={(v) => setF("event_type", v)} />
          <StatusSelect value={statusF} onChange={(v) => setF("status", v)} options={[
            ["", "Any status"], ["new", "New"], ["acknowledged", "Acknowledged"],
            ["resolved", "Resolved"], ["false_positive", "False positive"],
          ]} />
          <CameraSelect value={camera} onChange={(v) => setF("camera", v)} />
          <TagSelect value={tag} onChange={(v) => setF("tag", v)} />
          <StatusSelect value={order} onChange={(v) => setF("order", v)} options={[
            ["recent", "Newest first"], ["oldest", "Oldest first"], ["severity", "Worst first"],
          ]} />
          <input placeholder="Plate…" value={plateRaw} onChange={(e) => setPlateRaw(e.target.value)}
                 className="mono" style={{ width: 108 }} aria-label="Plate" />
        </div>

        {list.isError && <div style={{ padding: 12 }}><ErrorBox error={list.error} what="Timeline" /></div>}
        {list.isLoading && <Skeleton rows={9} />}

        <div className={`grow ${list.isFetching && !list.isLoading ? "stale" : ""}`} style={{ minHeight: 0 }}>
          {items.length === 0 && !list.isLoading ? (
            <Empty>No events match these filters.</Empty>
          ) : (
            <VirtualList items={items} estimate={74} style={{ height: "100%" }}
              render={(e) => (
                <button className={`item ${e.id === eventId ? "on" : ""}`} onClick={() => nav(`/timeline/${e.id}`)}>
                  <SevDot s={e.severity} />
                  {/* The crop, in the row itself.
                      Every one of these events carries a thumbnail and always
                      did — the API returns a working presigned URL for all 43.
                      The list simply never rendered them, so a violation queue
                      was a wall of identical sentences ("Person in 11
                      dolatpara-junagadh — live carriageway", eight times) with
                      nothing to tell one from another. An operator triaging
                      alerts is looking for the one that matters, and that
                      judgement is made on the picture, not the label.
                      Not zoomable here: the row is a button that opens the
                      event, and a click that sometimes opens a lightbox instead
                      would be worse than no lightbox. */}
                  <Thumb src={e.thumbnail_url} alt={e.label} className="sm" zoomable={false}
                         label={titleCase(e.event_type)} />
                  <span className="grow col" style={{ gap: 2 }}>
                    <span className="t trunc">{e.label}</span>
                    <span className="m">
                      {hhmmss(e.started_at)} · {e.camera_external_id} · {titleCase(e.event_type)}
                      {e.zone_name ? ` · ${e.zone_name}` : ""}
                    </span>
                    {e.tags.length > 0 && (
                      <span className="row wrap" style={{ gap: 3 }}>
                        {e.tags.slice(0, 4).map((t) => (
                          <span key={t.id} className="tag tiny" style={{ color: t.color }}><i />{t.name}</span>
                        ))}
                      </span>
                    )}
                  </span>
                  <span className="col" style={{ gap: 3, alignItems: "flex-end" }}>
                    <Status s={e.status} />
                    <span className="row tiny faint" style={{ gap: 4 }}>
                      {e.bookmarked && <span title="Bookmarked">★</span>}
                      {e.note_count > 0 && <span title={`${e.note_count} notes`}>✎{e.note_count}</span>}
                      {e.incident_ref && <span className="mono" title="In an incident">{e.incident_ref}</span>}
                    </span>
                  </span>
                </button>
              )} />
          )}
        </div>
        <Pager page={list.data} onPage={setOffset} />
      </div>

      <div className="pane-r">
        {eventId ? <Detail id={eventId} onTriaged={() => move(1)} /> : (
          <Empty>
            Select an event.<br />
            <span className="tiny">↑ ↓ move through the queue once one is open.</span>
          </Empty>
        )}
      </div>
      {rep ? null : null}
    </div>
  );
}

function Detail({ id, onTriaged }: { id: string; onTriaged: () => void }) {
  const q = useEvent(id);
  const rep = useReport();
  const triage = useTriage();
  const applyTags = useApplyTags();
  const removeTag = useRemoveTag();
  const addNote = useAddNote();
  const bookmark = useToggleBookmark();
  const [note, setNote] = useState("");
  const [tagText, setTagText] = useState("");
  const [promoting, setPromoting] = useState(false);

  if (q.isLoading) return <Skeleton rows={8} />;
  if (q.isError) return <div style={{ padding: 12 }}><ErrorBox error={q.error} what="Event" /></div>;

  const e: EventRow & Record<string, any> = q.data.event;
  const track = q.data.track;
  const notes = q.data.notes ?? [];
  const tags = q.data.tags ?? [];
  const incidents = q.data.incidents ?? [];
  const nearby = q.data.nearby_events ?? [];

  const setStatus = (status: string) =>
    triage.mutate({ id, status, note: note.trim() || undefined }, {
      onSuccess: () => { rep.ok(`Marked ${status.replace("_", " ")}`); setNote(""); onTriaged(); },
      onError: (err) => rep.err(err, "Triage"),
    });

  return (
    <div className="col" style={{ gap: 12, padding: 12 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        <SevDot s={e.severity} />
        <h1 className="grow trunc">{e.label}</h1>
        <Status s={e.status} />
        <Pill>{titleCase(e.event_type)}</Pill>
        <Pill k={e.source === "manual" ? "accent" : undefined}>{e.source}</Pill>
      </div>

      <Thumb src={e.thumbnail_url} alt={e.label} label="no crop stored for this event" />

      <div className="row wrap" style={{ gap: 6 }}>
        <button className="primary" disabled={triage.isPending} onClick={() => setStatus("acknowledged")}>Acknowledge</button>
        <button disabled={triage.isPending} onClick={() => setStatus("resolved")}>Resolve</button>
        {/* Not a delete. A false positive is the only record of what a threshold got
            wrong, and it is what tuning one is based on. */}
        <button className="danger" disabled={triage.isPending} onClick={() => setStatus("false_positive")}>False positive</button>
        <button className="ghost right" onClick={() =>
          bookmark.mutate({ target_type: "event", target_id: id, label: e.label.slice(0, 120), on: !e.bookmarked },
            { onSuccess: () => rep.ok(e.bookmarked ? "Bookmark removed" : "Bookmarked"), onError: (x) => rep.err(x) })}>
          {e.bookmarked ? "★ Saved" : "☆ Save"}
        </button>
        <button onClick={() => setPromoting(true)}>Add to incident…</button>
      </div>

      <dl className="kv">
        <dt>Camera</dt>
        <dd><Link to={`/cameras/${e.camera_external_id}`}>{e.camera_external_id}</Link> · {e.camera_name}</dd>
        <dt>Where</dt><dd>{[e.area, e.district].filter(Boolean).join(", ") || "—"}</dd>
        <dt>Started</dt><dd>{fullStamp(e.started_at)} <span className="faint">({ago(e.started_at)})</span></dd>
        {e.ended_at && <><dt>Ended</dt><dd>{fullStamp(e.ended_at)}</dd></>}
        {e.pts_ms != null && <><dt>Video offset</dt><dd>{dur(e.pts_ms / 1000)}</dd></>}
        {e.confidence != null && <><dt>Confidence</dt><dd>{pct(e.confidence, 1)}</dd></>}
        {e.zone_name && <><dt>Zone</dt><dd>{e.zone_name}</dd></>}
        {e.plate_text && <><dt>Plate</dt><dd><Plate text={e.plate_text} trust={e.plate_trust} origin={e.plate_origin}
                                       rtoKnown={e.rto_known} showOrigin /></dd></>}
        {e.reviewed_by && <><dt>Reviewed</dt><dd>{e.reviewed_by} · {stamp(e.reviewed_at)}</dd></>}
      </dl>

      {e.metrics && Object.keys(e.metrics).length > 0 && (
        <section className="card">
          <header><h4 className="grow">Why the detector fired</h4></header>
          <div className="body col" style={{ gap: 10 }}>
            {/* The geometry first, then the numbers that describe it. For a zone
                event the crop is 41x67 px on average — too small to show a
                person — but the rule's claim is about *where* the subject went,
                and that is drawable at any size. */}
            <SceneDiagram polygon={q.data.zone?.polygon} path={track?.path}
                          label={q.data.zone?.name} kind={e.event_type} />
            <dl className="kv">
              {Object.entries(e.metrics).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt>{titleCase(k)}</dt>
                  <dd>{typeof v === "number" ? (Number.isInteger(v) ? num(v) : v.toFixed(3)) : String(v)}</dd>
                </div>
              ))}
            </dl>
          </div>
        </section>
      )}

      {track && (
        <section className="card">
          <header>
            <h4 className="grow">Vehicle track</h4>
            <Link className="tiny" to={`/reconstruct?track=${track.id}`}>Reconstruct route →</Link>
          </header>
          <div className="body row" style={{ gap: 10, alignItems: "flex-start" }}>
            <div style={{ width: 120 }}><Thumb src={track.thumbnail_url} label="no crop" /></div>
            <dl className="kv grow">
              <dt>Class</dt><dd>{titleCase(track.class)} · {titleCase(track.dominant_color)}</dd>
              <dt>Seen</dt><dd>{hhmmss(track.first_seen)} → {hhmmss(track.last_seen)} ({dur(track.duration_s)})</dd>
              <dt>Frames</dt><dd>{num(track.detection_count)}</dd>
              {/* Consensus, not a single read: the measured single-read exact rate on this
                  footage has no standalone measurement, and the fold across a track is what makes a plate
                  usable at all. The read count is shown because it is the evidence. */}
              <dt>Plate</dt>
              <dd>{track.plate_text ? <Plate text={track.plate_text} trust={track.plate_trust}
                                          origin={track.plate_origin} rtoKnown={track.rto_known}
                                          showOrigin /> : "—"}</dd>
              {track.heading_deg != null && <><dt>Heading</dt><dd>{Math.round(track.heading_deg)}°</dd></>}
            </dl>
          </div>
        </section>
      )}

      <section className="card">
        <header><h4 className="grow">Tags</h4></header>
        <div className="body col">
          <div className="row wrap" style={{ gap: 4 }}>
            {tags.map((t: any) => (
              <span key={t.id} className="tag" style={{ color: t.color }}>
                <i />{t.name}
                <button className="ghost sm" style={{ padding: 0, marginLeft: 2, color: "inherit" }}
                        onClick={() => removeTag.mutate({ eventId: id, tag: t.name })} aria-label={`Remove ${t.name}`}>×</button>
              </span>
            ))}
            {tags.length === 0 && <span className="faint tiny">none</span>}
          </div>
          <form className="row" onSubmit={(ev) => {
            ev.preventDefault();
            const names = tagText.split(",").map((s) => s.trim()).filter(Boolean);
            if (!names.length) return;
            applyTags.mutate({ eventId: id, tags: names }, {
              onSuccess: () => { setTagText(""); rep.ok("Tagged"); }, onError: (x) => rep.err(x, "Tag"),
            });
          }}>
            {/* Apply-by-name creates on first use, so the shared vocabulary grows out of
                real work instead of an admin screen nobody opens. */}
            <input className="grow" placeholder="convoy, follow-up — comma separated"
                   value={tagText} onChange={(ev) => setTagText(ev.target.value)} />
            <button disabled={applyTags.isPending}>Apply</button>
          </form>
        </div>
      </section>

      <section className="card">
        <header>
          <h4 className="grow">Notes</h4>
          {e.alert_id && <span className="tiny faint">includes notes written on the alert</span>}
        </header>
        <div className="body col">
          {notes.map((n: any) => (
            <div key={n.id} className="col" style={{ gap: 2 }}>
              <div className="row tiny faint">
                <b>{n.operator}</b>
                <span>{stamp(n.created_at)}</span>
                {/* Provenance: the same occurrence gets written about from two doors. */}
                {n.target_type === "alert" && <Pill k="accent">from alert queue</Pill>}
              </div>
              <div style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
            </div>
          ))}
          {notes.length === 0 && <span className="faint tiny">No notes.</span>}
          <textarea rows={2} placeholder="Add a note — it carries into the handover."
                    value={note} onChange={(ev) => setNote(ev.target.value)} />
          <div className="row">
            <button className="right" disabled={!note.trim() || addNote.isPending}
                    onClick={() => addNote.mutate({ target_type: "event", target_id: id, body: note.trim() }, {
                      onSuccess: () => { setNote(""); rep.ok("Note saved"); }, onError: (x) => rep.err(x, "Note"),
                    })}>Save note</button>
          </div>
        </div>
      </section>

      {incidents.length > 0 && (
        <section className="card">
          <header><h4 className="grow">Incidents</h4></header>
          <div className="body col">
            {incidents.map((i: any) => (
              <Link key={i.id} to={`/incidents/${i.id}`} className="row small">
                <span className="mono">{i.ref}</span>
                <span className="grow trunc">{i.title}</span>
                <Status s={i.status} />
              </Link>
            ))}
          </div>
        </section>
      )}

      {nearby.length > 0 && (
        <section className="card">
          <header><h4 className="grow">Same camera, ±5 minutes</h4><span className="badge">{nearby.length}</span></header>
          <div className="list">
            {nearby.map((n: any) => (
              <Link key={n.id} to={`/timeline/${n.id}`} className="item" style={{ color: "inherit" }}>
                <SevDot s={n.severity} />
                <span className="grow trunc t">{n.label}</span>
                <span className="m">{hhmmss(n.started_at)}</span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {promoting && <PromoteModal eventId={id} onClose={() => setPromoting(false)} />}
    </div>
  );
}

/** Feature 3, from the operator's side: an event they are looking at becomes part of a
 *  case file, either an existing one or a new one named on the spot. */
function PromoteModal({ eventId, onClose }: { eventId: string; onClose: () => void }) {
  const incidents = useIncidents({ limit: 50, status: "open" });
  const add = useAddIncidentEvents();
  const rep = useReport();
  const [pick, setPick] = useState("");
  const [note, setNote] = useState("");

  return (
    <Modal title="Add to incident" onClose={onClose} footer={
      <>
        <button className="ghost" onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!pick || add.isPending}
                onClick={() => add.mutate({ id: pick, event_ids: [eventId], note: note.trim() || undefined }, {
                  onSuccess: () => { rep.ok("Added to incident"); onClose(); },
                  onError: (x) => rep.err(x, "Incident"),
                })}>Add</button>
      </>
    }>
      <label className="field">Incident
        <select value={pick} onChange={(e) => setPick(e.target.value)}>
          <option value="">Choose…</option>
          {(incidents.data?.items ?? []).map((i) => (
            <option key={i.id} value={i.id}>{i.ref} · {i.title}</option>
          ))}
        </select>
      </label>
      <label className="field">Why this event belongs (optional)
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="tiny faint">
        No open incident yet? Create one on the Incidents page, then come back — the
        sequence an investigator sets there is the case file's order, not the timestamp order.
      </div>
    </Modal>
  );
}
