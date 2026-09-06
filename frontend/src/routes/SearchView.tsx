import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useFacets, useSearch, useCameras, useDistricts, useCreateEvidence } from "../lib/queries";
import { dur, fullStamp, num, pct, stamp } from "../lib/format";
import { Empty, ErrorBox, Modal, Pager, Pill, Skeleton, Thumb } from "../components/ui";
import { useReport } from "../components/Toasts";
import { useDebounced } from "../components/Filters";

/**
 * Feature 4 — one form, because the API is one endpoint.
 *
 * The reason it is one form is the same reason the endpoint is one endpoint: an operator
 * does not know in advance whether what they remember is a plate fragment, a colour, a
 * place or a phrase, and asking them to choose a tab is asking them to answer the
 * question they came here with.
 *
 * What the panel under the results is for: the server reports back how it read the
 * request — which words it consumed as structured filters, whether the plate matched
 * exactly or by trigram, whether the residual went to CLIP at all. Without that, a
 * search that quietly ignored half the query looks identical to a search that ran it.
 */

const LEVELS = [
  ["track", "Vehicles", "one row per vehicle passing a camera"],
  ["detection", "Frames", "one row per frame — for picking an evidence crop"],
] as const;

export default function SearchView() {
  const [sp, setSp] = useSearchParams();
  const report = useReport();
  const facets = useFacets();
  const cams = useCameras();
  const districts = useDistricts();

  const [q, setQ] = useState(sp.get("q") ?? "");
  const [plate, setPlate] = useState(sp.get("plate") ?? "");
  const [camera, setCamera] = useState<string[]>(sp.getAll("camera"));
  const [district, setDistrict] = useState<string[]>(sp.getAll("district"));
  const [classes, setClasses] = useState<string[]>(sp.getAll("class"));
  const [colors, setColors] = useState<string[]>(sp.getAll("color"));
  const [from, setFrom] = useState(sp.get("from") ?? "");
  const [to, setTo] = useState(sp.get("to") ?? "");
  const [minConf, setMinConf] = useState("");
  const [level, setLevel] = useState<"track" | "detection">(
    (sp.get("level") as "track" | "detection") ?? "track");
  const [offset, setOffset] = useState(0);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);

  const dq = useDebounced(q, 350);
  const dplate = useDebounced(plate, 300);

  const body = useMemo(() => {
    const b: Record<string, unknown> = { level, limit: 60, offset };
    if (dq.trim()) b.q = dq.trim();
    if (dplate.trim()) b.plate = dplate.trim();
    if (camera.length) b.camera_ids = camera;
    if (district.length) b.districts = district;
    if (classes.length) b.classes = classes;
    if (colors.length) b.colors = colors;
    if (from) b.from_ts = new Date(from).toISOString();
    if (to) b.to_ts = new Date(to).toISOString();
    if (minConf) b.min_plate_confidence = Number(minConf);
    // An unfiltered search is 52k detections the operator did not ask for. Every other
    // filter is optional; *some* filter is not.
    const filtered = Object.keys(b).some((k) => !["level", "limit", "offset"].includes(k));
    return filtered ? b : null;
  }, [dq, dplate, camera, district, classes, colors, from, to, minConf, level, offset]);

  // Mirror the form into the URL so a result set can be pasted into a handover note.
  useEffect(() => {
    const next = new URLSearchParams();
    if (dq) next.set("q", dq);
    if (dplate) next.set("plate", dplate);
    camera.forEach((c) => next.append("camera", c));
    district.forEach((d) => next.append("district", d));
    classes.forEach((c) => next.append("class", c));
    colors.forEach((c) => next.append("color", c));
    if (from) next.set("from", from);
    if (to) next.set("to", to);
    if (level !== "track") next.set("level", level);
    setSp(next, { replace: true });
  }, [dq, dplate, camera, district, classes, colors, from, to, level, setSp]);

  const res = useSearch(body);
  const items = res.data?.items ?? [];
  const meta = res.data?.query;
  const cov = facets.data?.coverage;

  const toggle = (set: string[], v: string, fn: (x: string[]) => void) =>
    fn(set.includes(v) ? set.filter((x) => x !== v) : [...set, v]);

  const pick = (id: string) => setPicked((prev) => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <div className="view col" style={{ gap: 12 }}>
      <section className="card">
        <div className="body col" style={{ gap: 10 }}>
          <div className="row wrap" style={{ gap: 8 }}>
            <input value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }}
                   placeholder='e.g. "white truck near Adalaj" — colour and class words become filters, the rest goes to CLIP'
                   style={{ flex: 2, minWidth: 260 }} aria-label="Description" />
            <input value={plate} onChange={(e) => { setPlate(e.target.value); setOffset(0); }}
                   placeholder="Plate or fragment" className="mono" style={{ width: 150 }}
                   aria-label="Plate" />
            <select value={level} onChange={(e) => setLevel(e.target.value as any)} aria-label="Level"
                    title={LEVELS.find((l) => l[0] === level)?.[2]}>
              {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>

          <div className="row wrap" style={{ gap: 8 }}>
            <label className="field">From
              <input type="datetime-local" value={from} onChange={(e) => { setFrom(e.target.value); setOffset(0); }} />
            </label>
            <label className="field">To
              <input type="datetime-local" value={to} onChange={(e) => { setTo(e.target.value); setOffset(0); }} />
            </label>
            <label className="field">Min plate confidence
              <select value={minConf} onChange={(e) => setMinConf(e.target.value)}>
                <option value="">any</option>
                {["0.5", "0.7", "0.85"].map((v) => <option key={v} value={v}>{pct(+v, 0)}</option>)}
              </select>
            </label>
            <label className="field">District
              <select value={district[0] ?? ""} onChange={(e) => setDistrict(e.target.value ? [e.target.value] : [])}>
                <option value="">any</option>
                {(districts.data?.districts ?? []).map((d: any) =>
                  <option key={d.district} value={d.district}>{d.district}</option>)}
              </select>
            </label>
            <label className="field">Camera
              <select value={camera[0] ?? ""} onChange={(e) => setCamera(e.target.value ? [e.target.value] : [])}>
                <option value="">any</option>
                {(cams.data?.items ?? []).map((c) =>
                  <option key={c.id} value={c.external_id}>{c.external_id} — {c.name}</option>)}
              </select>
            </label>
            {cov?.from_ts && (
              <span className="right tiny faint" style={{ maxWidth: 220 }}>
                Indexed footage covers {stamp(cov.from_ts)} → {stamp(cov.to_ts)}. Nothing outside that
                window has been searched yet.
              </span>
            )}
          </div>

          <div className="row wrap" style={{ gap: 5 }}>
            {(facets.data?.classes ?? []).map((f) => (
              <button key={f.value} className={`pill ${classes.includes(f.value) ? "accent" : ""}`}
                      onClick={() => { toggle(classes, f.value, setClasses); setOffset(0); }}>
                {f.value} <span className="faint">{num(f.n)}</span>
              </button>
            ))}
          </div>
          <div className="row wrap" style={{ gap: 5 }}>
            {(facets.data?.colors ?? []).map((f) => (
              <button key={f.value} className={`pill ${colors.includes(f.value) ? "accent" : ""}`}
                      onClick={() => { toggle(colors, f.value, setColors); setOffset(0); }}>
                {f.value} <span className="faint">{num(f.n)}</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      {meta && (
        <div className="row wrap small" style={{ gap: 6 }}>
          <span className="faint">Read as:</span>
          <Pill k={meta.plate_mode === "fuzzy" ? "warn" : meta.plate_mode === "exact" ? "ok" : undefined}>
            plate {meta.plate_mode}
          </Pill>
          {meta.hints_extracted && (
            <Pill k="accent">
              consumed {[...(meta.colors ?? []), ...(meta.classes ?? [])].join(" + ")} from the text
            </Pill>
          )}
          {meta.vector_search
            ? <Pill k="accent">CLIP on “{meta.residual_text}”</Pill>
            : <Pill>no vector search</Pill>}
          <span className="faint mono tiny">{meta.took_ms?.toFixed(0)} ms</span>
          {meta.plate_mode === "fuzzy" && (
            <span className="tiny faint">
              No exact consensus match, so these are trigram-near reads — one or two OCR
              confusions away. Single-read exact accuracy on this footage is 17.6% against
              85.2% for the consensus plate, which is why fuzzy exists rather than an empty result.
            </span>
          )}
        </div>
      )}

      {res.isError && <ErrorBox error={res.error} what="Search" />}
      {body === null && (
        <Empty>
          Add at least one filter. An unfiltered search returns the whole index, which is
          not a search.
        </Empty>
      )}
      {res.isFetching && items.length === 0 && <Skeleton rows={8} />}
      {body !== null && !res.isFetching && items.length === 0 && (
        <Empty>Nothing matched. Widen the time window or drop the plate — it may only exist as a fuzzy read.</Empty>
      )}

      {items.length > 0 && (
        <>
          <div className="row small">
            <span>{res.data.count} {level === "track" ? "vehicles" : "frames"}
              {res.data.has_more ? "+" : ""}</span>
            {picked.size > 0 && (
              <span className="right row" style={{ gap: 6 }}>
                <span className="faint">{picked.size} selected</span>
                <button className="primary sm" onClick={() => setSaving(true)}>Snapshot as evidence</button>
                <button className="ghost sm" onClick={() => setPicked(new Set())}>Clear</button>
              </span>
            )}
          </div>

          <div className={`grid ${level === "track" ? "g3" : "g4"}`}>
            {items.map((it: any) => (
              <div key={it.id} className={`item col ${picked.has(it.id) ? "on" : ""}`} style={{ gap: 6 }}>
                <label className="check tiny" style={{ gap: 5 }}>
                  <input type="checkbox" checked={picked.has(it.id)} onChange={() => pick(it.id)} />
                  <span className="mono">{it.camera_external_id}</span>
                  <span className="right faint">{level === "track" ? stamp(it.first_seen) : stamp(it.ts)}</span>
                </label>
                <Thumb src={it.thumbnail_url} label={it.class ?? "crop"} />
                <div className="row" style={{ gap: 6 }}>
                  {it.plate_text
                    ? <b className="mono">{it.plate_text}</b>
                    : <span className="faint tiny">no plate read</span>}
                  {it.plate_confidence != null && <span className="tiny faint">{pct(it.plate_confidence, 0)}</span>}
                  {level === "track" && it.plate_read_count > 0 && (
                    <span className="right tiny faint" title="Reads that voted on this consensus">
                      {it.plate_read_count}×
                    </span>
                  )}
                </div>
                <div className="tiny faint trunc">
                  {[it.dominant_color, it.class].filter(Boolean).join(" ") || "unclassified"}
                  {level === "track" && it.duration_s != null && ` · ${dur(it.duration_s)}`}
                  {level === "detection" && it.confidence != null && ` · det ${pct(it.confidence, 0)}`}
                </div>
                <div className="tiny faint trunc" title={fullStamp(level === "track" ? it.first_seen : it.ts)}>
                  {[it.area, it.district].filter(Boolean).join(", ")}
                </div>
                <div className="row tiny" style={{ gap: 6 }}>
                  <Link to={`/cameras/${it.camera_external_id}`}>Camera</Link>
                  <Link to={`/investigate?${level === "track" ? `track=${it.id}` : `detection=${it.id}`}`}>
                    Follow →
                  </Link>
                </div>
              </div>
            ))}
          </div>

          <Pager page={{ offset, limit: 60, count: res.data.count, has_more: res.data.has_more }}
                 onPage={setOffset} />
        </>
      )}

      {saving && (
        <SnapshotModal
          level={level}
          ids={[...picked]}
          onClose={() => setSaving(false)}
          onDone={(n) => { report.ok(`Evidence snapshot saved with ${n} items`); setPicked(new Set()); setSaving(false); }}
          onFail={(e) => report.err(e)}
        />
      )}
    </div>
  );
}

/**
 * Feature 5 — an evidence snapshot taken straight off a result set.
 *
 * It records the *ids*, not the crops. The point of a snapshot is that it still resolves
 * to the same rows in six weeks; copying thumbnails would create a second copy of the
 * imagery that then has to be kept and secured, which is exactly the centralised video
 * storage the deployment is built to avoid.
 */
function SnapshotModal({ level, ids, onClose, onDone, onFail }: {
  level: "track" | "detection";
  ids: string[];
  onClose: () => void;
  onDone: (n: number) => void;
  onFail: (e: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const m = useCreateEvidence();

  const submit = () => {
    m.mutate(
      {
        title: title.trim() || `Search snapshot — ${ids.length} ${level}s`,
        note: note.trim(),
        track_ids: level === "track" ? ids : [],
        detection_ids: level === "detection" ? ids : [],
      },
      { onSuccess: () => onDone(ids.length), onError: (e: any) => onFail(e?.message ?? "Save failed") },
    );
  };

  return (
    <Modal title="Snapshot as evidence" onClose={onClose}
           footer={<>
             <button className="ghost" onClick={onClose}>Cancel</button>
             <button className="primary" onClick={submit} disabled={m.isPending}>
               {m.isPending ? "Saving…" : `Save ${ids.length}`}
             </button>
           </>}>
      <label className="field">Title
        <input value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder={`Search snapshot — ${ids.length} ${level}s`} />
      </label>
      <label className="field">Note
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)}
                  placeholder="What this set shows and why it was kept." />
      </label>
      <p className="tiny faint">
        Stores references to {ids.length} {level === "track" ? "tracks" : "detections"}, not copies of
        the crops — the snapshot resolves to the same rows later without this console holding
        a second copy of departmental imagery.
      </p>
    </Modal>
  );
}
