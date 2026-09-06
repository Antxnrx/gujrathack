/** Formatting the control room reads, in the control room's timezone. */

const TZ = "Asia/Kolkata";

const clock = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: TZ,
});
const dayClock = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ,
});
const full = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium", timeStyle: "medium", timeZone: TZ,
});

export function hhmmss(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(+d) ? "—" : clock.format(d);
}

export function stamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(+d) ? "—" : dayClock.format(d);
}

export function fullStamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(+d) ? "—" : full.format(d);
}

/** "4m ago". Coarse on purpose — a control room acts on minutes, not milliseconds. */
export function ago(ts: string | null | undefined, now = Date.now()): string {
  if (!ts) return "—";
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.round((now - t) / 1000);
  if (s < 0) return "in " + dur(-s);
  if (s < 45) return "just now";
  return dur(s) + " ago";
}

export function dur(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${Math.round((s % 3600) / 60)}m`;
}

export function pct(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

export function num(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return x.toLocaleString("en-IN");
}

/** A likelihood ratio, which is the unit the reconstruction actually speaks in. */
export function lr(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  if (x >= 100) return `${Math.round(x)}×`;
  if (x >= 10) return `${x.toFixed(1)}×`;
  return `${x.toFixed(2)}×`;
}

export function titleCase(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
