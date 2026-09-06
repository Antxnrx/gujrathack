import { useState } from "react";
import { Link } from "react-router-dom";
import { useEvidence, useEvidenceItem, useEvidenceVerify } from "../lib/queries";
import { ago, fullStamp, num, stamp } from "../lib/format";
import { Empty, ErrorBox, Pill, Skeleton, Spinner } from "../components/ui";

/**
 * Feature 5 — evidence snapshots, and the chain-of-custody check on top of them.
 *
 * A snapshot is a manifest: the rows as they read at the moment it was sealed, plus a
 * SHA-256 over a canonical serialisation of them. It stores references and field values,
 * not copies of the footage — which is what lets the deployment keep its promise that
 * departmental video is never centralised, while still producing something an
 * investigator can hand over.
 *
 * The verify panel reports two independent facts, and conflating them would be a serious
 * error:
 *
 *   - **Seal intact** — the manifest still hashes to the digest recorded when it was
 *     sealed. This is about the snapshot.
 *   - **Drift** — fields in the live database now differ from what was sealed. This is
 *     about the world, and it is *not* tampering. An event that was `new` when sealed and
 *     is `acknowledged` today is a normal, correct thing to have happened.
 *
 * An intact seal with drift is the ordinary case for any snapshot older than a shift.
 * Reporting it as a failure would train operators to ignore the one signal that matters.
 */
export default function Evidence() {
  const q = useEvidence({ limit: 60 });
  const [open, setOpen] = useState<string | null>(null);
  const snaps = q.data?.snapshots ?? [];

  return (
    <div className="view col" style={{ gap: 14 }}>
      <div className="row">
        <div className="col" style={{ gap: 2 }}>
          <b>Evidence snapshots</b>
          <span className="tiny faint">
            Each one records the rows as they read when it was sealed, plus a digest over
            them. No imagery is copied — the departmental systems keep their footage.
          </span>
        </div>
        <span className="right small dim">{q.data?.count ?? 0} sealed</span>
      </div>

      {q.isError && <ErrorBox error={q.error} what="Evidence" />}
      {q.isLoading && <Skeleton rows={5} />}
      {snaps.length === 0 && !q.isLoading && (
        <Empty>
          Nothing sealed yet. Select results in Search, or save a confirmed route from a
          reconstruction.
        </Empty>
      )}

      <div className="col" style={{ gap: 10 }}>
        {snaps.map((s: any) => (
          <section key={s.id} className="card">
            <header>
              <span className="mono">{s.ref}</span>
              <b className="trunc">{s.title}</b>
              <span className="grow" />
              {s.incident_ref && (
                <Link className="pill accent" to={`/incidents/${s.incident_id}`}>{s.incident_ref}</Link>
              )}
              <span className="tiny faint">{num(s.item_count)} items · {ago(s.created_at)}</span>
              <button className="ghost sm" onClick={() => setOpen(open === s.id ? null : s.id)}>
                {open === s.id ? "Hide" : "Verify & inspect"}
              </button>
            </header>
            <div className="body col" style={{ gap: 6 }}>
              <dl className="kv tiny">
                <dt>Sealed</dt><dd>{fullStamp(s.created_at)}</dd>
                <dt>By</dt><dd>{s.operator}</dd>
                <dt>Digest</dt>
                <dd style={{ wordBreak: "break-all" }} title={s.sha256}>{s.sha256}</dd>
              </dl>
              {open === s.id && <Panel id={s.id} />}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function Panel({ id }: { id: string }) {
  const v = useEvidenceVerify(id);
  const d = useEvidenceItem(id);

  if (v.isError) return <ErrorBox error={v.error} what="Verification" />;
  if (v.isLoading) return <Spinner label="Recomputing the digest…" />;

  const r = v.data;
  const intact = r?.manifest_intact === true;
  const drift: any[] = r?.drift ?? [];
  const items = d.data?.manifest?.items ?? {};

  return (
    <div className="col" style={{ gap: 11, marginTop: 4 }}>
      <div className="row wrap" style={{ gap: 7 }}>
        <Pill k={intact ? "ok" : "danger"}>{intact ? "Seal intact" : "SEAL BROKEN"}</Pill>
        <Pill k={drift.length ? "warn" : undefined}>
          {drift.length ? `${drift.length} field${drift.length === 1 ? "" : "s"} drifted since` : "no drift"}
        </Pill>
        <span className="tiny faint">
          sealed {stamp(r.sealed_at)} by {r.sealed_by?.name ?? "unknown"} ({r.sealed_by?.kind})
        </span>
      </div>

      <div className="tiny faint">
        {intact
          ? "The manifest still hashes to the digest recorded at sealing, so its contents are unaltered."
          : "The recomputed digest does not match the recorded one. The manifest has been altered since sealing."}
      </div>

      {!intact && (
        <dl className="kv tiny">
          <dt>Recorded</dt><dd style={{ wordBreak: "break-all" }}>{r.recorded_sha256}</dd>
          <dt>Recomputed</dt><dd style={{ wordBreak: "break-all", color: "var(--critical)" }}>{r.recomputed_sha256}</dd>
        </dl>
      )}

      {drift.length > 0 && (
        <div className="col" style={{ gap: 5 }}>
          <div className="small">
            <b>What changed in the database since</b> — this is not tampering. The snapshot is
            unaltered; the live rows moved on, which is what a case file is supposed to survive.
          </div>
          <table className="tbl">
            <thead>
              <tr><th>Kind</th><th>Field</th><th>Sealed as</th><th>Now</th></tr>
            </thead>
            <tbody>
              {drift.map((x, i) => (
                <tr key={i}>
                  <td>{x.kind}</td>
                  <td className="mono">{x.field}</td>
                  <td className="mono faint">{String(x.sealed)}</td>
                  <td className="mono">{String(x.current)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {d.isLoading && <Skeleton rows={3} />}
      {Object.entries(items).some(([, v]) => Array.isArray(v) && v.length > 0) && (
        <div className="col" style={{ gap: 7 }}>
          <b className="small">Manifest contents</b>
          {Object.entries(items).map(([kind, rows]: [string, any]) => (
            Array.isArray(rows) && rows.length > 0 && (
              <div key={kind} className="col" style={{ gap: 4 }}>
                <div className="tiny faint">{kind} · {rows.length}</div>
                {rows.slice(0, 8).map((row: any) => (
                  <div key={row.id} className="row tiny item" style={{ gap: 7 }}>
                    <span className="mono faint">{String(row.id).slice(0, 8)}</span>
                    <span className="trunc grow">
                      {row.label ?? row.plate_text ?? row.class ?? kind}
                    </span>
                    {row.camera_uid && <span className="mono faint">{row.camera_uid}</span>}
                    {(row.started_at ?? row.ts ?? row.first_seen) && (
                      <span className="faint">{stamp(row.started_at ?? row.ts ?? row.first_seen)}</span>
                    )}
                  </div>
                ))}
                {rows.length > 8 && <span className="tiny faint">…and {rows.length - 8} more</span>}
              </div>
            )
          ))}
        </div>
      )}
    </div>
  );
}
