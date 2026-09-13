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
 * A stored crop.
 *
 * `thumbnail_url` is a presigned MinIO URL the API attaches per row, and it is
 * null wherever a crop was never written. A bare `<img>` with a null src renders
 * a broken icon; this renders what the frame *was*, which is more use.
 *
 * These are vehicle crops, and a vehicle photographed from the front is taller
 * than it is wide — the mean aspect across this estate is 0.82, and 93% of the
 * 62,000 crops are narrower than 16:9. The first version of this component put
 * every one of them in a 16:9 box with `object-fit: cover`, which scales to fill
 * the width and then discards the overflow top and bottom: **48% of a typical
 * crop, cut symmetrically from the two ends.**
 *
 * The number plate is at the bottom of a front-on vehicle crop. So the one thing
 * an officer opens the picture to read was the first thing thrown away, on every
 * screen in the console, and the image looked deliberate rather than broken.
 *
 * `contain` shows the whole crop. The letterboxing that costs on a portrait
 * image is the correct trade: empty space is honest, a silently cropped plate is
 * not. Clicking opens the crop full size, because a 84px-wide thumbnail is for
 * recognising a vehicle and a plate needs magnification to read.
 */
export function Thumb({ src, alt, label, className, zoomable = true }:
  { src?: string | null; alt?: string; label?: string; className?: string;
    zoomable?: boolean }) {
  const [failed, setFailed] = useState(false);
  const [zoom, setZoom] = useState(false);
  useEffect(() => setFailed(false), [src]);
  if (!src || failed) {
    return <div className={`thumb ph ${className ?? ""}`}>{label ?? "no crop"}</div>;
  }
  return (
    <>
      <img className={`thumb ${zoomable ? "zoomable" : ""} ${className ?? ""}`}
           src={src} alt={alt ?? ""} loading="lazy" decoding="async"
           title={zoomable ? "Click to see the full crop" : undefined}
           onError={() => setFailed(true)}
           onClick={zoomable ? () => setZoom(true) : undefined} />
      {zoom && <Lightbox src={src} alt={alt ?? label ?? "crop"} onClose={() => setZoom(false)} />}
    </>
  );
}

/** The crop at its own size, over a dimmed page. Escape or a click closes it. */
function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="lightbox" onClick={onClose} role="dialog" aria-label={alt}>
      <figure onClick={(e) => e.stopPropagation()}>
        <img src={src} alt={alt} />
        <figcaption>
          <span>{alt}</span>
          <button className="ghost sm" onClick={onClose}>Close</button>
        </figcaption>
      </figure>
    </div>
  );
}

/**
 * What the vehicle looks like, with the parts the system is unsure of marked.
 *
 * Colour and class are CLIP zero-shot over the whole crop, and on this estate
 * that is measurably unreliable: a black Maruti S-Cross was labelled "white
 * truck" — the orange cloth on its dashboard read through the windscreen, and
 * COCO has no class between car and truck for an SUV. Next to a correctly-read
 * plate, that one wrong phrase is what made the whole system look broken.
 *
 * Nothing is hidden. A weak colour is still a useful search filter and the
 * operator can open the crop and see for themselves. What changes is that the
 * row stops *claiming* to have observed what it actually inferred: a contested
 * class reads `vehicle`, and a contested colour is dimmed and marked.
 */
export function Appearance(
  { colour, klass, colourUncertain, classUncertain, opinions }:
  { colour?: string | null; klass?: string | null;
    colourUncertain?: boolean; classUncertain?: boolean;
    opinions?: Record<string, unknown> | null },
) {
  if (!colour && !klass) return <span className="faint">—</span>;
  const why = classUncertain && opinions
    ? `Two classifiers disagreed (${Object.entries(opinions)
        .map(([k, v]) => `${k}: ${v}`).join(", ")}), so only "vehicle" is supported`
    : undefined;
  return (
    <span className="appearance">
      {colour && (
        <span className={colourUncertain ? "unsure" : undefined}
              title={colourUncertain ? "The bodywork reads a different colour — treat as an estimate" : undefined}>
          {colour}{colourUncertain && <i className="q">?</i>}
        </span>
      )}
      {klass && <span className={classUncertain ? "unsure" : undefined} title={why}>{klass}</span>}
    </span>
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

/**
 * A plate, shown with how far it can be trusted.
 *
 * Measured on this estate, 89% of consensus plates sit in the confidence band
 * that was never once exactly right against human-read ground truth, and 2% sit
 * in the band that was always right. Rendering all of them as the same
 * monospace string tells an operator something false — and a plate is the field
 * most likely to be acted on, because it reads as a fact in a way a percentage
 * does not.
 *
 * The text is never hidden. A weak read is still a lead worth following, and the
 * operator looking at the crop is better placed to judge it than a threshold is.
 * What changes is that an unreliable read now looks unreliable.
 */
export function Plate(
  { text, trust, origin, rtoKnown, showOrigin = false, className = "" }:
  {
    text?: string | null;
    trust?: { tier?: string; label?: string; guidance?: string } | null;
    /** "GJ 11 · Junagadh" — the registering authority, read off the plate itself. */
    origin?: string | null;
    /** False when the RTO code is one Gujarat has never issued. */
    rtoKnown?: boolean;
    showOrigin?: boolean;
    className?: string;
  },
) {
  if (!text) return <span className="faint">—</span>;
  const tier = trust?.tier ?? "unknown";
  // An unissued RTO code is a reason to distrust the read, so it belongs in the
  // same tooltip as the confidence tiering rather than in a separate affordance
  // the operator has to go looking for.
  const note = [
    trust ? `${trust.label} — ${trust.guidance}` : null,
    origin,
    rtoKnown === false ? "No RTO is registered under this code — treat the read as suspect" : null,
  ].filter(Boolean).join("\n") || undefined;
  return (
    <span className={`plate plate-${tier} ${className}`} title={note}>
      <span className="mono">{text}</span>
      {tier === "verify" && <i className="plate-flag">unverified</i>}
      {tier === "lead" && <i className="plate-flag">low confidence</i>}
      {rtoKnown === false && <i className="plate-flag plate-flag-bad">unknown RTO</i>}
      {showOrigin && origin && <i className="plate-origin">{origin}</i>}
    </span>
  );
}
