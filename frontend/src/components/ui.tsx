import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../lib/api";
import { hhmmss, titleCase } from "../lib/format";
import type { RegistrationCheck, Severity, Tag } from "../lib/types";

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

/**
 * What the rule actually saw, drawn — because the photograph cannot show it.
 *
 * A zone intrusion asserts a geometric fact: this subject was inside this
 * polygon, for this long. The evidence stored for it is a crop of the subject,
 * and on this estate those subjects average **41x67 px** and go down to 15 px
 * wide, because a pedestrian in the carriageway is far from the lens. 33 of 43
 * events have a subject under 60 px. No full frames were ever written — only
 * crops — so there is no photograph of the scene to fall back on.
 *
 * The zone polygon and the track's path are both stored in normalised frame
 * coordinates, so the *claim* can be drawn at any size even though the picture
 * cannot be enlarged. That is the honest presentation: this is a diagram of what
 * the detector measured, not a photograph of what happened, and it tells an
 * operator the one thing the crop cannot — where the subject went relative to
 * the carriageway.
 */
export function SceneDiagram(
  { polygon, path, label, kind }:
  { polygon?: number[][] | null;
    path?: { x: number; y: number; w?: number; h?: number }[] | null;
    label?: string; kind?: string },
) {
  const poly = (polygon ?? []).filter((p) => Array.isArray(p) && p.length >= 2);
  const pts = (path ?? []).filter((p) => typeof p?.x === "number" && typeof p?.y === "number");
  if (poly.length < 3 && pts.length < 2) return null;

  const d = poly.map(([x, y]) => `${x * 100},${y * 100}`).join(" ");
  const line = pts.map((p) => `${p.x * 100},${p.y * 100}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <figure className="scene">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img"
           aria-label={`${label ?? "scene"} — subject path against the zone`}>
        {poly.length >= 3 && <polygon points={d} className="scene-zone" />}
        {pts.length >= 2 && <polyline points={line} className="scene-path" />}
        {pts.map((p, i) => (
          <circle key={i} cx={p.x * 100} cy={p.y * 100} r="0.7" className="scene-pt" />
        ))}
        {last && <circle cx={last.x * 100} cy={last.y * 100} r="1.8" className="scene-now" />}
      </svg>
      <figcaption>
        <span className="scene-key"><i className="k-zone" /> {label ?? kind ?? "zone"}</span>
        <span className="scene-key"><i className="k-path" /> subject path · {pts.length} points</span>
      </figcaption>
    </figure>
  );
}

/**
 * The plate checked against the vehicle it is registered to.
 *
 * The record is the obvious half. The findings are the half worth the feature: a
 * registration cannot change category, so a motorcycle's number read off a truck
 * is a cloned plate, not a misreading — and nothing in the footage alone can
 * establish that. Findings lead, sorted hardest first, because an operator
 * scanning this panel is looking for the reason to act.
 *
 * When no register is configured this renders the reason rather than an empty
 * box or an error. That is the normal state of a force that has not yet been
 * granted VAHAN access, and it is not a fault they can fix from this screen.
 */
export function RegistrationCheckPanel({ check }: { check: RegistrationCheck | undefined }) {
  if (!check) return null;
  if (!check.available) {
    return (
      <section className="card reg-check">
        <header><h4 className="grow">Vehicle register</h4><span className="pill">not connected</span></header>
        <div className="body small dim">{check.reason}</div>
      </section>
    );
  }
  const r = check.registration;
  const rank = { critical: 0, high: 1, medium: 2, low: 3, info: 4 } as const;
  const findings = [...check.findings].sort(
    (a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9));
  return (
    <section className="card reg-check">
      <header>
        <h4 className="grow">Vehicle register</h4>
        {!r?.authoritative && <span className="pill warn">not authoritative</span>}
        <span className="tiny faint">{check.provider}</span>
      </header>
      <div className="body col" style={{ gap: 10 }}>
        {findings.map((f, i) => (
          <div key={i} className={`finding sev-${f.severity}`}>
            <b>{f.kind.replace(/_/g, " ")}</b>
            {(f.observed || f.registered) && (
              <div className="tiny mono">
                filmed <i>{f.observed ?? "—"}</i> · registered <i>{f.registered ?? "—"}</i>
              </div>
            )}
            <div className="small">{f.means}</div>
          </div>
        ))}
        {r && (
          <dl className="kv">
            {r.maker && <><dt>Vehicle</dt><dd>{[r.maker, r.model].filter(Boolean).join(" ")}</dd></>}
            {r.vehicle_class && <><dt>Class</dt><dd>{r.vehicle_class}</dd></>}
            {r.colour && <><dt>Colour</dt><dd>{r.colour}</dd></>}
            {r.fuel && <><dt>Fuel</dt><dd>{r.fuel}</dd></>}
            {r.registered_on && <><dt>Registered</dt><dd>{r.registered_on}</dd></>}
            {r.rto_name && <><dt>Issuing RTO</dt><dd>{r.rto_name}</dd></>}
            {r.status && <><dt>Status</dt><dd>{r.status}</dd></>}
          </dl>
        )}
        {check.plate_trust && check.plate_trust.tier !== "actionable" && (
          <p className="tiny warn-text">
            This check rests on a plate the console marks “{check.plate_trust.label}”.
            Confirm the characters against the crop before acting on any mismatch.
          </p>
        )}
      </div>
    </section>
  );
}

/**
 * "?" — what this panel is, on hover.
 *
 * Nearly every number in this console is derived rather than counted, and the
 * derivation is the part an operator needs in order to trust it. "Awaiting
 * triage" is not "all events", detector output is per rule and not per camera,
 * and a plate's confidence band means something specific that was measured.
 * Writing that next to each figure would bury the figure; hiding it entirely
 * leaves an operator guessing.
 *
 * CSS-only on hover *and* focus, so it works from the keyboard, and it carries
 * the text in `title` as well so a screen reader and a touch device both get it.
 * No positioning library, no portal — a sibling span and one absolute rule.
 */
export function Help({ children }: { children: ReactNode }) {
  const text = typeof children === "string" ? children : undefined;
  return (
    <span className="help" tabIndex={0} role="note" aria-label={text} title={text}>
      ?<span className="help-bubble">{children}</span>
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
        {/* A disabled control should say why it is disabled — "Prev" greyed out
            with no explanation reads as broken rather than as "you are on the
            first page". */}
        <button className="sm" disabled={offset === 0}
                title={offset === 0 ? "You are on the first page" : "Previous page"}
                onClick={() => onPage(Math.max(0, offset - limit))}>Prev</button>
        <button className="sm" disabled={!more}
                title={!more ? "No more results" : "Next page"}
                onClick={() => onPage(offset + limit)}>Next</button>
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
