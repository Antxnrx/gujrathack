import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { Empty, ErrorBox, Plate, Skeleton, Thumb } from "../components/ui";
import { useReport } from "../components/Toasts";
import { fullStamp } from "../lib/format";
import type { PlaybookResult, PlaybookRow, PlaybookSpec } from "../lib/types";
import { usePlaybooks } from "../lib/queries";

/**
 * The console, arranged by the job instead of by the table.
 *
 * Every other route here is a view onto one kind of row — events, tracks,
 * alerts, cameras. That is the right shape for someone who already knows what
 * they are looking for and the wrong shape for someone arriving with a job. An
 * officer does not want to browse detections; they want to know whether a
 * stolen vehicle has been through, or who was outside a shop at eleven o'clock.
 *
 * Each playbook is one form and one answer. The chaining — watchlist, then a
 * retrospective scan, then alerts, then an incident — happens server-side in
 * backend/app/routers/playbooks.py, so the steps are the same however the
 * officer arrives at them and the audit log records the job rather than five
 * unrelated calls.
 *
 * The caveats panel is not decoration and is deliberately given as much room as
 * the result. A canvass that returns nothing may mean the vehicle was not there
 * or may mean none of those cameras can resolve a plate; an officer told only
 * "no results" cannot tell which, and will act on the wrong one.
 */

type Which = "wanted-vehicle" | "scene-canvass" | "court-pack";

/** ISO string for a datetime-local input, trimmed to the minute. */
const isoLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  .toISOString().slice(0, 16);

export default function Playbooks() {
  const cat = usePlaybooks();
  const [open, setOpen] = useState<Which | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PlaybookResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const report = useReport();

  async function run(which: Which, body: unknown) {
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await api.post<PlaybookResult>(`/playbooks/${which}`, body);
      setResult(r);
      report.ok(`${which.replace("-", " ")} complete`);
    } catch (e) {
      setError(e);
      report.err(e, "playbook");
    } finally {
      setBusy(false);
    }
  }

  if (cat.isLoading) return <Skeleton rows={4} />;
  if (cat.error) return <ErrorBox error={cat.error} what="playbooks" />;

  return (
    <div className="view col" style={{ gap: 14 }}>
      <header className="col" style={{ gap: 4 }}>
        <h2 style={{ margin: 0 }}>What do you need to do?</h2>
        <p className="small dim" style={{ margin: 0, maxWidth: "62ch" }}>
          Each of these runs a fixed sequence over the estate and leaves a case file
          behind. The steps are the same every time and are recorded in the audit log
          as one job.
        </p>
      </header>

      <div className="playbook-grid">
        {(cat.data?.playbooks ?? []).map((p: PlaybookSpec) => {
          const runnable = p.id !== "follow-vehicle";
          return (
            <article key={p.id} className={`playbook-card ${open === p.id ? "on" : ""}`}>
              <h3>{p.name}</h3>
              <p className="playbook-q">{p.question}</p>
              <p className="small dim">{p.when}</p>
              <ul className="playbook-produces">
                {p.produces.map((x) => <li key={x}>{x}</li>)}
              </ul>
              <span className="tiny faint">Needs: {p.requires}</span>
              {runnable ? (
                <button
                  className={open === p.id ? "ghost" : "primary"}
                  onClick={() => { setOpen(open === p.id ? null : (p.id as Which)); setResult(null); setError(null); }}
                >
                  {open === p.id ? "Close" : "Start"}
                </button>
              ) : (
                <Link className="btn ghost" to="/reconstruct">Open reconstruction</Link>
              )}
            </article>
          );
        })}
      </div>

      {open === "wanted-vehicle" && <WantedForm busy={busy} onRun={(b) => run(open, b)} />}
      {open === "scene-canvass" && <CanvassForm busy={busy} onRun={(b) => run(open, b)} />}
      {open === "court-pack" && <CourtPackForm busy={busy} onRun={(b) => run(open, b)} />}

      {error ? <ErrorBox error={error} what="this playbook" /> : null}
      {busy ? <Skeleton rows={3} /> : null}
      {result ? <Result r={result} /> : null}
    </div>
  );
}

/* ── Forms ──────────────────────────────────────────────────────────────── */

function WantedForm({ busy, onRun }: { busy: boolean; onRun: (b: unknown) => void }) {
  const [plate, setPlate] = useState("");
  const [reason, setReason] = useState("");
  return (
    <form
      className="card col playbook-form"
      onSubmit={(e) => { e.preventDefault(); onRun({ plate, reason, priority: "high" }); }}
    >
      <label htmlFor="pb-plate">Registration</label>
      <input id="pb-plate" value={plate} required placeholder="GJ11CO5760"
             onChange={(e) => setPlate(e.target.value.toUpperCase())} />
      <p className="tiny dim">
        Type it as it appears on the vehicle. History is matched on confusion distance,
        not on an exact string, so a plate the reader recorded one character out is still
        found — and the result says which sightings were exact and which were inferred.
      </p>

      <label htmlFor="pb-reason">Why it is wanted</label>
      <input id="pb-reason" value={reason} required
             placeholder="Stolen vehicle — FIR 221/2026, Junagadh City"
             onChange={(e) => setReason(e.target.value)} />

      <button className="primary" disabled={busy || !plate || !reason}>
        Search the archive and arm the watchlist
      </button>
    </form>
  );
}

function CanvassForm({ busy, onRun }: { busy: boolean; onRun: (b: unknown) => void }) {
  const [cameras, setCameras] = useState("cam06, cam09");
  // Off by default: a canvass is a read any signed-in role may run, and opening
  // a case file is a write that not every role can. Defaulting it on would make
  // the common read fail with a permissions error for an analyst.
  const [openIncident, setOpenIncident] = useState(false);
  const [from, setFrom] = useState(isoLocal(new Date(Date.parse("2026-09-05T00:00:00Z"))));
  const [to, setTo] = useState(isoLocal(new Date(Date.parse("2026-09-05T01:00:00Z"))));
  return (
    <form
      className="card col playbook-form"
      onSubmit={(e) => {
        e.preventDefault();
        onRun({
          cameras: cameras.split(",").map((s) => s.trim()).filter(Boolean),
          from_ts: new Date(from).toISOString(),
          to_ts: new Date(to).toISOString(),
          open_incident: openIncident,
        });
      }}
    >
      <label htmlFor="pb-cams">Cameras</label>
      <input id="pb-cams" value={cameras} required placeholder="cam06, cam09"
             onChange={(e) => setCameras(e.target.value)} />

      <div className="row wrap" style={{ gap: 12 }}>
        <div className="col" style={{ gap: 4 }}>
          <label htmlFor="pb-from">From</label>
          <input id="pb-from" type="datetime-local" value={from}
                 onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="col" style={{ gap: 4 }}>
          <label htmlFor="pb-to">To</label>
          <input id="pb-to" type="datetime-local" value={to}
                 onChange={(e) => setTo(e.target.value)} />
        </div>
      </div>
      <p className="tiny dim">
        The result is ordered by how far each plate can be trusted, not by time — so the
        vehicles worth acting on are at the top rather than the ones that happened to
        pass first. Vehicles with no readable plate are kept, last: a make, a colour and
        a time is still what a witness statement is made of.
      </p>
      <label className="check">
        <input type="checkbox" id="pb-open-inc" checked={openIncident}
               onChange={(e) => setOpenIncident(e.target.checked)} />
        Open a case file for what this finds (needs the operator role)
      </label>
      <button className="primary" disabled={busy}>Canvass this window</button>
    </form>
  );
}

function CourtPackForm({ busy, onRun }: { busy: boolean; onRun: (b: unknown) => void }) {
  const [id, setId] = useState("");
  return (
    <form className="card col playbook-form"
          onSubmit={(e) => { e.preventDefault(); onRun({ incident_id: id }); }}>
      <label htmlFor="pb-inc">Incident</label>
      <input id="pb-inc" value={id} required placeholder="incident id"
             onChange={(e) => setId(e.target.value)} />
      <p className="tiny dim">
        Seals references and field values, never copies of imagery, then verifies the seal
        immediately — a mismatch found now is a bug someone can fix, where the same
        mismatch found in a courtroom is indistinguishable from tampering.
        <br />
        <Link to="/incidents">Pick one from Incidents ↗</Link>
      </p>
      <button className="primary" disabled={busy || !id}>Seal and verify</button>
    </form>
  );
}

/* ── Result ─────────────────────────────────────────────────────────────── */

function Result({ r }: { r: PlaybookResult }) {
  const sightings = r.found?.sightings ?? [];
  const vehicles = r.found?.vehicles ?? [];
  const rows = sightings.length ? sightings : vehicles;
  const snapshot = r.created?.snapshot;
  const verification = r.created?.verification;

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="card col" style={{ gap: 10 }}>
        <div className="row wrap" style={{ gap: 16 }}>
          {r.found?.sighting_count !== undefined && (
            <Fact n={r.found.sighting_count} l="past sightings" />
          )}
          {r.found?.exact !== undefined && <Fact n={r.found.exact} l="exact reads" />}
          {r.found?.inferred !== undefined && <Fact n={r.found.inferred} l="inferred" />}
          {r.found?.vehicle_count !== undefined && (
            <Fact n={r.found.vehicle_count} l="vehicles present" />
          )}
          {r.found?.people_count ? <Fact n={r.found.people_count} l="people present" /> : null}
          {r.found?.with_readable_plate !== undefined && (
            <Fact n={r.found.with_readable_plate} l="with a plate" />
          )}
          {r.found?.actionable_plates !== undefined && (
            <Fact n={r.found.actionable_plates} l="actionable" />
          )}
          {r.created?.alerts_raised !== undefined && (
            <Fact n={r.created.alerts_raised} l="alerts raised" />
          )}
        </div>

        {r.created?.incident && (
          <p className="small">
            Case file opened:{" "}
            <Link to={`/incidents/${r.created.incident.id}`}>
              {r.created.incident.ref} — {r.created.incident.title}
            </Link>
          </p>
        )}
        {snapshot && (
          <p className="small">
            Sealed <b>{snapshot.ref}</b> over {snapshot.item_count} items ·{" "}
            <span className={verification?.manifest_intact ? "ok" : "bad"}>
              {verification?.manifest_intact ? "manifest intact" : "MANIFEST BROKEN"}
            </span>
            {verification?.drift_count ? `, ${verification.drift_count} rows drifted` : ", no drift"}
            <br />
            <code className="tiny mono">{snapshot.sha256}</code>
          </p>
        )}
      </div>

      {/* Caveats sit above the rows, not below them: what could be wrong with an
          answer has to be read before the answer is acted on, not after. */}
      {r.caveats?.length ? (
        <section className="card caveats col" style={{ gap: 8 }}>
          <h4>Before you act on this</h4>
          {r.caveats.map((c, i) => <p key={i} className="small">{c}</p>)}
        </section>
      ) : null}

      {rows.length ? (
        <div className="card">
          <table className="tbl">
            <thead>
              <tr>
                <th />
                <th>Plate</th>
                <th>Match</th>
                <th>What</th>
                <th>Camera</th>
                <th>Seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t: PlaybookRow) => (
                <tr key={t.track_id ?? t.id}>
                  <td style={{ width: 84 }}>
                    <Thumb src={t.thumbnail_url} alt={t.plate_text ?? "vehicle"} />
                  </td>
                  <td>
                    <Plate text={t.plate_text} trust={t.plate_trust}
                           origin={t.plate_origin} rtoKnown={t.rto_known} showOrigin />
                  </td>
                  <td className="small">
                    {t.match === "confusion"
                      ? <span className="pill warn" title="Matched one confusion-class character away from the plate you typed">
                          inferred {t.match_score?.toFixed(3)}
                        </span>
                      : t.match === "exact" ? <span className="pill">exact</span> : <span className="faint">—</span>}
                  </td>
                  <td className="small">
                    {[t.dominant_color, t.class].filter(Boolean).join(" ") || <span className="faint">—</span>}
                  </td>
                  <td className="small mono">{t.camera_external_id}</td>
                  <td className="small">{fullStamp(t.first_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>Nothing found in the archive. Read the caveats above — on this estate
          that is often a statement about the cameras rather than about the vehicle.</Empty>
      )}

      {r.next_steps?.length ? (
        <section className="card col" style={{ gap: 6 }}>
          <h4>Next</h4>
          <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
            {r.next_steps.map((s, i) => <li key={i}>{s}</li>)}
          </ol>
        </section>
      ) : null}
    </div>
  );
}

function Fact({ n, l }: { n: number; l: string }) {
  return (
    <div className="col" style={{ gap: 2 }}>
      <b className="mono" style={{ fontSize: 22 }}>{n}</b>
      <span className="tiny dim">{l}</span>
    </div>
  );
}
