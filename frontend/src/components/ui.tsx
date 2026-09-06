import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../lib/api";
import { hhmmss, titleCase } from "../lib/format";
import type { Severity, Tag } from "../lib/types";

/** Small pieces used everywhere. Kept in one file because each is under twenty lines
 *  and a directory of twelve one-component files costs more to navigate than it saves. */

export function Spinner({ label }: { label?: string }) {
  return <div className="empty">{label ?? "Loading…"}</div>;
}

export function Skeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="col" style={{ padding: 12 }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skel" style={{ height: 34, opacity: 1 - i * 0.09 }} />
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Errors say what failed *and* what it means when the cause is environmental.
 *  A 503 from the stream relay is the sandbox gateway, not a bug in the console, and an
 *  operator who is told that stops looking in the wrong place. */
export function ErrorBox({ error, what }: { error: unknown; what?: string }) {
  const e = error as ApiError | Error;
  const status = e instanceof ApiError ? e.status : 0;
  const hint =
    status === 503 ? "The upstream service is unreachable — this is reported, not enforced."
    : status === 502 ? "An upstream returned something unexpected."
    : status === 404 ? null
    : status === 0 ? "The API did not answer. Is the backend running on :8000?"
    : null;
  return (
    <div className="errbox">
      <div><b>{what ? `${what} failed` : "Request failed"}</b>{status ? ` · ${status}` : ""}</div>
      <div style={{ marginTop: 3 }}>{e?.message ?? String(error)}</div>
      {hint && <div className="tiny faint" style={{ marginTop: 5 }}>{hint}</div>}
    </div>
  );
}

export function SevDot({ s }: { s: Severity | string | null }) {
  return <i className={`dot ${s ?? "info"}`} title={titleCase(s)} />;
}

export function Pill({ k, children }: { k?: string; children: ReactNode }) {
  return <span className={`pill ${k ?? ""}`}>{children}</span>;
}

export function Status({ s }: { s: string | null }) {
  if (!s) return null;
  return <span className={`pill ${s}`}>{titleCase(s)}</span>;
}

export function TagChip({ tag, onRemove }: { tag: Tag; onRemove?: () => void }) {
  return (
    <span className="tag" style={{ color: tag.color }}>
      <i />
      {tag.name}
      {onRemove && (
        <button className="ghost sm" style={{ padding: 0, marginLeft: 2, color: "inherit" }}
                onClick={(e) => { e.stopPropagation(); onRemove(); }} aria-label={`Remove ${tag.name}`}>×</button>
      )}
    </span>
  );
}

export function Stat({ n, l, tone }: { n: ReactNode; l: string; tone?: "hot" | "warm" | "cool" }) {
  return (
    <div className={`card stat ${tone ?? ""}`}>
      <div className="n">{n}</div>
      <div className="l">{l}</div>
    </div>
  );
}

/**
 * Thumbnails, and the reason this is not a bare `<img>`.
 *
 * `thumbnail_url` is a presigned MinIO URL that the API attaches per row, and it is
 * null whenever a crop was never written — which in this deployment is every row,
 * because the bucket does not exist yet. A bare img with src={null} renders a broken
 * icon; this renders what the frame *was*, which is more use than a broken icon and
 * does not change once imagery lands.
 */
export function Thumb({ src, alt, label, className }:
  { src?: string | null; alt?: string; label?: string; className?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  if (!src || failed) {
    return <div className={`thumb ph ${className ?? ""}`}>{label ?? "no crop"}</div>;
  }
  return (
    <img className={`thumb ${className ?? ""}`} src={src} alt={alt ?? ""}
         loading="lazy" decoding="async" onError={() => setFailed(true)} />
  );
}

export function Bar({ value, max, color }: { value: number; max: number; color?: string }) {
  const w = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return <div className="bar"><i style={{ width: `${w}%`, background: color }} /></div>;
}

export function Modal({ title, onClose, children, footer }:
  { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLElement>("input,textarea,select,button")?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <header><h2 className="grow">{title}</h2><button className="ghost" onClick={onClose} aria-label="Close">×</button></header>
        <div className="body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function Pager({ page, onPage }:
  { page: { offset: number; limit: number; count: number; has_more?: boolean; total?: number } | undefined;
    onPage: (offset: number) => void }) {
  if (!page) return null;
  const { offset, limit, count, has_more, total } = page;
  const more = has_more ?? (total !== undefined ? offset + count < total : false);
  return (
    <div className="row small dim" style={{ padding: "8px 12px" }}>
      <span>
        {count === 0 ? "no rows" : `${offset + 1}–${offset + count}`}
        {total !== undefined ? ` of ${total}` : ""}
      </span>
      <span className="right row">
        <button className="sm" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}>Prev</button>
        <button className="sm" disabled={!more} onClick={() => onPage(offset + limit)}>Next</button>
      </span>
    </div>
  );
}

/** A relative clock that ticks without re-rendering its parent's list. */
export function Clock() {
  const [now, setNow] = useState(() => new Date().toISOString());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date().toISOString()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return <span className="mono dim small">{hhmmss(now)} IST</span>;
}
