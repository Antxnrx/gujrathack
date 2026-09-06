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

/**
 * Feature 12 — multi-frame ANPR, shown as the per-character vote it actually is.
 *
 * `scripts/score_anpr.py` measured 17.6% exact accuracy for a single read against 85.2%
 * for the consensus. This is the picture of why: each column is one plate position, and
 * the winning character is the one whose weighted votes across frames beat the others.
 * A column where the runner-up is close is a column the operator should not trust.
 */
export function PlateVote({ votes }: { votes: any }) {
  const positions: any[] = votes?.positions ?? [];
  if (!positions.length) return null;
  const raw: string[] = votes?.raw ?? [];

  return (
    <div className="col" style={{ gap: 8 }}>
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
      {raw.length > 0 && (
        <div className="row wrap tiny" style={{ gap: 4 }}>
          <span className="faint">Individual reads:</span>
          {raw.map((r, i) => <span key={i} className="pill mono">{r}</span>)}
        </div>
      )}
    </div>
  );
}
