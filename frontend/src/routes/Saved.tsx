import { useState } from "react";
import { Link } from "react-router-dom";
import { useBookmarks, useDeleteNote, useNotes, useToggleBookmark } from "../lib/queries";
import { ago, fullStamp, titleCase } from "../lib/format";
import { Empty, ErrorBox, Pager, Pill, SevDot, Skeleton, Thumb } from "../components/ui";
import { useReport } from "../components/Toasts";

/**
 * Features 6 and 7 — bookmarks and operator notes, on one page because they are the two
 * halves of a shift handover.
 *
 * Both are polymorphic: a bookmark or a note attaches to an event, an alert, a track, a
 * camera, an incident or a pursuit. The list therefore has to route by `target_type`
 * rather than assuming everything is an event, and the `target` object the API embeds is
 * what makes that possible without a fetch per row.
 *
 * Notes are shown newest-first and never edited in place here. A note is a record of what
 * someone believed at a time; correcting it belongs on the thing it is attached to, where
 * the context that prompted it is visible.
 */

const ROUTE: Record<string, (id: string) => string> = {
  event: (id) => `/timeline/${id}`,
  alert: (id) => `/alerts?open=${id}`,
  incident: (id) => `/incidents/${id}`,
  pursuit: (id) => `/reconstruct/${id}`,
  track: (id) => `/investigate?track=${id}`,
  camera: (id) => `/cameras/${id}`,
  detection: (id) => `/search?detection=${id}`,
};

const linkFor = (type: string, id: string) => ROUTE[type]?.(id) ?? "#";

export default function Saved() {
  const [tab, setTab] = useState<"bookmarks" | "notes">("bookmarks");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const report = useReport();

  const bm = useBookmarks({ target_type: kind || undefined });
  const nq = useNotes({ target_type: kind || undefined, limit: 50, offset });
  const toggle = useToggleBookmark();
  const del = useDeleteNote();

  return (
    <div className="view col" style={{ gap: 12 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <div className="row" style={{ gap: 4 }}>
          {(["bookmarks", "notes"] as const).map((t) => (
            <button key={t} className={tab === t ? "primary" : "ghost"} onClick={() => { setTab(t); setOffset(0); }}>
              {titleCase(t)}
            </button>
          ))}
        </div>
        <select value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }} aria-label="Attached to">
          <option value="">Anything</option>
          {["event", "alert", "track", "camera", "incident", "pursuit", "detection"].map((k) =>
            <option key={k} value={k}>{titleCase(k)}s</option>)}
        </select>
        <span className="right small dim">
          {tab === "bookmarks" ? `${bm.data?.count ?? 0} bookmarked` : `${nq.data?.count ?? 0} notes`}
        </span>
      </div>

      {tab === "bookmarks" ? (
        <>
          {bm.isError && <ErrorBox error={bm.error} what="Bookmarks" />}
          {bm.isLoading && <Skeleton rows={5} />}
          {(bm.data?.items ?? []).length === 0 && !bm.isLoading && (
            <Empty>
              Nothing bookmarked. Bookmark an event or an alert to carry it across a shift
              change without leaving a note on it.
            </Empty>
          )}
          <div className="grid g2">
            {(bm.data?.items ?? []).map((b: any) => (
              <div key={b.id} className="card">
                <div className="body row" style={{ gap: 10, alignItems: "flex-start" }}>
                  <div style={{ width: 110, flex: "0 0 110px" }}>
                    <Thumb src={b.target?.thumbnail_url} label={b.target_type} />
                  </div>
                  <div className="grow col" style={{ gap: 5, minWidth: 0 }}>
                    <div className="row" style={{ gap: 6 }}>
                      <Pill k="accent">{b.target_type}</Pill>
                      {b.target?.severity && <SevDot s={b.target.severity} />}
                      <span className="right tiny faint">{ago(b.created_at)}</span>
                    </div>
                    <Link to={linkFor(b.target_type, b.target_id)} className="trunc small">
                      {b.target?.title ?? b.target_id}
                    </Link>
                    {b.target?.subtitle && <div className="tiny faint">{b.target.subtitle}</div>}
                    {b.target?.camera_external_id && (
                      <div className="tiny faint">
                        <span className="mono">{b.target.camera_external_id}</span>{" "}
                        {b.target.camera_name}
                        {b.target.ts && ` · ${fullStamp(b.target.ts)}`}
                      </div>
                    )}
                    {b.label && <div className="small" style={{ color: "var(--warn)" }}>{b.label}</div>}
                    <div className="row tiny" style={{ gap: 6 }}>
                      <span className="faint">by {b.operator}</span>
                      <button className="ghost sm right"
                              onClick={() => toggle.mutate(
                                { target_type: b.target_type, target_id: b.target_id, on: false },
                                { onSuccess: () => report.ok("Bookmark removed"),
                                  onError: (e: any) => report.err(e?.message ?? "Failed") })}>
                        Remove
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        <>
          {nq.isError && <ErrorBox error={nq.error} what="Notes" />}
          {nq.isLoading && <Skeleton rows={6} />}
          {(nq.data?.items ?? []).length === 0 && !nq.isLoading && (
            <Empty>No note yet. Notes written while triaging show up here too.</Empty>
          )}
          <div className="col" style={{ gap: 8 }}>
            {(nq.data?.items ?? []).map((n: any) => (
              <div key={n.id} className="item col" style={{ gap: 5 }}>
                <div className="row wrap tiny" style={{ gap: 7 }}>
                  <Pill k="accent">{n.target_type}</Pill>
                  <Link to={linkFor(n.target_type, n.target_id)} className="mono faint">
                    {String(n.target_id).slice(0, 8)}
                  </Link>
                  <b>{n.operator}</b>
                  <span className="faint">{fullStamp(n.created_at)}</span>
                  {n.updated_at && n.updated_at !== n.created_at && (
                    <span className="faint">edited {ago(n.updated_at)}</span>
                  )}
                  <button className="ghost sm right"
                          onClick={() => del.mutate(n.id, {
                            onSuccess: () => report.ok("Note deleted"),
                            onError: (e: any) => report.err(e?.message ?? "Failed"),
                          })}>×</button>
                </div>
                <div className="small" style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
              </div>
            ))}
          </div>
          <Pager page={nq.data} onPage={setOffset} />
        </>
      )}
    </div>
  );
}
