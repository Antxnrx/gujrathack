import { useEffect, useState } from "react";
import { useCameras, useFacets, useTags } from "../lib/queries";

/**
 * The filter bar shared by the timeline, search and the camera index.
 *
 * `useDebounced` is the only reason free-text filtering is usable: the events query is a
 * twenty-column join with four EXISTS subqueries, and firing it per keystroke queues
 * requests faster than Postgres retires them. 250 ms is below the threshold where typing
 * feels laggy and above the interval between keystrokes.
 */
export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function CameraSelect({ value, onChange, allowAll = true }:
  { value: string; onChange: (v: string) => void; allowAll?: boolean }) {
  const { data } = useCameras();
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Camera">
      {allowAll && <option value="">All cameras</option>}
      {(data?.items ?? []).map((c) => (
        <option key={c.external_id} value={c.external_id}>
          {c.external_id} · {c.name}
        </option>
      ))}
    </select>
  );
}

export function EventTypeSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data } = useFacets();
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Event type">
      <option value="">All types</option>
      {(data?.event_types ?? []).map((t) => (
        <option key={t.value} value={t.value}>{t.value.replace(/_/g, " ")} ({t.n})</option>
      ))}
    </select>
  );
}

export function TagSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data } = useTags();
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Tag">
      <option value="">Any tag</option>
      {(data?.tags ?? []).map((t) => (
        <option key={t.id} value={t.name}>{t.name}{t.usage ? ` (${t.usage})` : ""}</option>
      ))}
    </select>
  );
}

export function StatusSelect({ value, onChange, options }:
  { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Status">
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}

/** A local-datetime input bound to an ISO string, which is what every span filter wants. */
export function TimeInput({ value, onChange, label }:
  { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <label className="field">
      {label}
      <input type="datetime-local" value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}
