import { lr as fmtLr, pct } from "../lib/format";

/**
 * The score breakdown, rendered as what it is: a set of independent likelihood ratios.
 *
 * This component exists because the number an operator must not be given is a bare
 * "87% match". The engine does not produce one — it produces a factor per line of
 * evidence, each answering "how many times more likely is this observation if it is the
 * subject than if it is not", and those factors can and do disagree. The candidate this
 * was built against scores 23.5x overall while its *plate* argues 0.148x against, because
 * two confident reads conflict. A single percentage would hide exactly the fact an
 * investigator needs to see before acting.
 *
 * So: bars grow right from a centre line when a factor argues for, left when it argues
 * against, on a log scale because likelihood ratios multiply. 1.0 is the null — no
 * information — and lands on the centre.
 */

const FACTORS = ["time", "plate", "color", "visual"] as const;

const WHAT: Record<string, string> = {
  time: "Arrival time against the corridor's expected transit",
  plate: "Plate reads compared, when both sides have one",
  color: "Dominant colour against how common it is at this camera",
  visual: "CLIP appearance against a background of same-class vehicles here",
};

export function LrBars({ breakdown }: { breakdown: any }) {
  if (!breakdown) return null;
  const factors = FACTORS.filter((k) => breakdown[k] && typeof breakdown[k].lr === "number");
  // Log scale, clamped at 100x either way — beyond that the bar length stops carrying
  // information and only the number does.
  const span = (v: number) => Math.max(-1, Math.min(1, Math.log10(Math.max(v, 1e-6)) / 2));

  return (
    <div className="col" style={{ gap: 4 }}>
      {factors.map((k) => {
        const f = breakdown[k];
        const s = span(f.lr);
        const against = f.lr < 1;
        return (
          <div key={k} className="lrbar" title={`${WHAT[k]}\n${f.calibration ?? ""}`}>
            <span className="k">{k}</span>
            <span className="track">
              <i className={against ? "against" : "for"} style={{ width: `${Math.abs(s) * 50}%` }} />
              <span className="mid" />
            </span>
            <span className="v" style={{ color: against ? "var(--critical)" : undefined }}>
              {fmtLr(f.lr)}
            </span>
          </div>
        );
      })}
      <div className="tiny faint" style={{ marginTop: 2 }}>
        Bars are likelihood ratios on a log scale — right argues for the match, left against.
        1× is the centre and means the factor carries no information.
      </div>
      {factors.map((k) => {
        const f = breakdown[k];
        if (!f.note && f.verdict !== "conflict") return null;
        return (
          <div key={`${k}-n`} className="tiny" style={{ color: "var(--warn)" }}>
            {k}: {f.note ?? f.verdict}
          </div>
        );
      })}
    </div>
  );
}

/** The prior and the two gates, stated separately so neither is mistaken for the other. */
export function ScoreHeader({ candidate }: { candidate: any }) {
  const sb = candidate.score_breakdown ?? {};
  const prior = sb.prior ?? {};
  return (
    <div className="col" style={{ gap: 5 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        <span className="pill accent" title="How many times more consistent with the subject than with chance. This is the gate that decides whether a lead is worth your attention.">
          {fmtLr(candidate.evidence_ratio)} evidence
        </span>
        <span className="pill" title="Folds in the corridor prior and the size of the candidate pool. Good for ranking, poor for thresholding — a real match through a weak corridor at a busy camera scores low and is still the lead worth following.">
          {pct(candidate.score, 1)} posterior
        </span>
        {candidate.match_kind && <span className="pill">{candidate.match_kind}</span>}
        {candidate.hops > 1 && <span className="pill warn">{candidate.hops} hops</span>}
      </div>
      {sb.interpretation && <div className="small">{sb.interpretation}</div>}
      <div className="tiny faint">
        Prior {pct(prior.probability ?? 0, 1)} from a {prior.corridor_evidence ?? "?"} corridor
        {prior.corridor_observations ? ` (n=${prior.corridor_observations})` : " (no observed transits)"}
        {prior.candidate_pool != null && ` · pool of ${prior.candidate_pool}`}
        {prior.pool_truncated && " (truncated)"}
        {sb.transit_s != null && ` · travelled in ${Math.round(sb.transit_s)}s`}
      </div>
      {prior.reference_population && (
        <div className="tiny faint">Reference set: {prior.reference_population}</div>
      )}
    </div>
  );
}

/** How the consensus reached its answer, in terms an operator can act on. */
const SNAP_NOTE: Record<string, string> = {
  argmax: "the vote's own answer — already a legal plate, nothing was changed",
  beam_snap: "a character was traded for its runner-up to reach a legal plate",
  validator_fuzzy: "characters were substituted that the vote did not support",
  unsnapped: "not a legal plate in any form — treat this as a partial read",
  single_read: "one frame only; capped at 0.28 confidence by design",
};

/**
 * Feature 12 — multi-frame ANPR, shown as the per-character vote it actually is.
 *
 * A plate is the one field an operator acts on directly: put on a watchlist,
 * radioed to a unit, typed into a registry. `GJ11BR2912 · 66%` is not enough to
 * decide on — ten frames agreeing on every character and two frames agreeing on
 * six both round to the same number and are not the same evidence.
 *
 * So the ballot is shown. Each column is one plate position and the winning
 * character is the one whose weighted votes across frames beat the others; an
 * amber column is one where the runner-up was nearly as well supported, which is
 * exactly where the plate is most likely wrong and exactly what an operator
 * needs in order to search both spellings.
 *
 * Two things are surfaced beyond the columns, because both change how far a read
 * should be trusted and neither is visible in the string itself:
 *
 *   - **how much repair it took.** A plate that was legal as the vote cast it is
 *     worth more than one that needed a character traded to become legal, and
 *     the cost of that trade is recorded in nats.
 *   - **whether the tail is short.** Indian registrations are issued with four
 *     trailing digits, so a three-digit tail is usually a dropped character
 *     rather than a short plate. Acting on `GJ11AB457` when the vehicle is
 *     `GJ11AB4570` means searching a registry for something never issued.
 */
export function PlateVote(
  { votes, plate, trust }: { votes: any; plate?: string | null; trust?: any },
) {
  const positions: any[] = votes?.positions ?? [];
  if (!positions.length) return null;
  const raw: string[] = votes?.raw ?? [];
  const snap: string | undefined = votes?.snap_method;
  const cost: number = votes?.snap_cost ?? 0;

  // Mirrors ml/plate_validator.is_probably_truncated. Kept as a display hint
  // rather than a second source of truth — the API remains authoritative.
  const tail = (plate ?? "").match(/(\d{1,4})$/)?.[1] ?? "";
  const shortTail = Boolean(plate) && tail.length > 0 && tail.length < 4;

  return (
    <div className="col" style={{ gap: 8 }}>
      {/* The decision, above the evidence for it. An operator scanning a queue
          needs to know whether to act before they need to know why. */}
      {trust && (
        <div className={`trust trust-${trust.tier}`}>
          <b>{trust.label}</b>
          <span className="tiny">{trust.guidance}</span>
          <span className="tiny faint">
            measured: {trust.measured} · fifteen hand-read plates
          </span>
        </div>
      )}

      <div className="platevote">
        {positions.map((p) => {
          const top = p.top ?? [];
          const winner = top[0]?.[1] ?? 0;
          const runner = top[1]?.[1] ?? 0;
          // A margin under a third means the runner-up was nearly as well supported.
          const shaky = winner > 0 && runner / winner > 0.33;
          return (
            <div key={p.pos} className={`pv ${shaky ? "shaky" : ""}`}
                 title={top.map((t: any[]) => `${t[0]}  ${t[1].toFixed(2)}`).join("\n")}>
              <b>{p.winner}</b>
              <span className="tiny faint">{winner.toFixed(1)}</span>
              {top.slice(1, 3).map((t: any[], i: number) => (
                <span key={i} className="tiny faint alt">{t[0]} {t[1].toFixed(1)}</span>
              ))}
            </div>
          );
        })}
      </div>

      <div className="tiny faint">
        {votes.reads} reads voted per position ({votes.method}); amber columns are ones where
        the runner-up character was nearly as well supported.
      </div>

      {snap && (
        <div className="row wrap tiny" style={{ gap: 6 }}>
          <span className={`pill ${votes.format_valid ? "" : "warn"}`}>
            {snap.replace(/_/g, " ")}
          </span>
          <span className="faint">{SNAP_NOTE[snap] ?? ""}</span>
          {cost > 0 && <span className="faint">· cost {cost.toFixed(2)} nats</span>}
        </div>
      )}

      {shortTail && (
        <div className="row wrap tiny" style={{ gap: 6 }}>
          <span className="pill warn">partial</span>
          <span className="faint">
            {tail.length} trailing digit{tail.length === 1 ? "" : "s"} where a current
            Indian registration carries four — a character was probably dropped.
          </span>
        </div>
      )}

      {raw.length > 0 && (
        <div className="row wrap tiny" style={{ gap: 4 }}>
          <span className="faint">Individual reads:</span>
          {raw.map((r, i) => <span key={i} className="pill mono">{r}</span>)}
        </div>
      )}
    </div>
  );
}
