#!/usr/bin/env python3
"""Score multi-frame ANPR consensus against the seeder's ground truth.

Why this exists
---------------
``ml/analytics/anpr.py`` argues that eleven bad reads of one plate carry more
information than the best single read. That is a claim, and a claim about
accuracy is worth exactly as much as the measurement behind it.

``scripts/seed_demo.py --report`` writes the plate it *intended* for every
synthetic track, alongside the corrupted per-frame reads the OCR simulator
produced from it. So the ground truth is known, which makes consensus recovery a
number rather than an argument:

    single-read accuracy   what one frame gets right
    consensus accuracy     what the vote gets right
    lift                   the difference — the whole reason anpr.py exists

Two things this deliberately does *not* do:

* It does not score against the raw reads it was given. The reads come from
  ``_OCR_SWAPS`` in the seeder, which is intentionally a wider and differently
  shaped confusion table than anpr's ``_CONFUSION_CLASSES``, with an unmodelled
  random tail. If the solver were scored against its own error model the result
  would be a tautology.
* It does not average confidence and call that calibration. A confidence number
  is only useful if it *predicts* correctness, so accuracy is bucketed by
  reported confidence and the buckets are printed side by side. A model that is
  90% confident should be right about 90% of the time; if it isn't, the number
  is decoration and an operator should not be shown it.

Usage:
    python -m scripts.score_anpr --session-key demo-7
    python -m scripts.score_anpr --session-key demo-7 --report artifacts/seed_demo_report.json
    python -m scripts.score_anpr --session-key demo-7 --show-failures 15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

from ml.db import get_connection
from ml.analytics.anpr import plate_similarity

DEFAULT_REPORT = "artifacts/seed_demo_report.json"


def char_errors(pred: str, truth: str) -> int:
    """Levenshtein distance — plain, not confusion-weighted.

    Character error rate is meant to be the unforgiving measure. The
    confusion-aware score is reported separately as ``similarity``; mixing the
    two would let the solver's own notion of "close enough" grade its own work.
    """
    if pred == truth:
        return 0
    prev = list(range(len(truth) + 1))
    for i, pc in enumerate(pred, 1):
        cur = [i] + [0] * len(truth)
        for j, tc in enumerate(truth, 1):
            cur[j] = min(prev[j - 1] + (pc != tc), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def load_truth(path: str) -> dict[str, dict[str, dict]]:
    if not os.path.exists(path):
        sys.exit(f"No ground-truth report at {path}. "
                 f"Run: python -m scripts.seed_demo --report")
    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)
    truth = report.get("plate_truth")
    if not truth:
        sys.exit(f"{path} has no plate_truth section — was it written by an older seeder?")
    return truth


def fetch_tracks(conn, session_key: str) -> list[dict]:
    """Consensus results, joined to the camera external_id the truth table keys on."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.external_id, t.track_id, t.class, t.dominant_color,
                   t.plate_text, t.plate_confidence, t.plate_read_count, t.plate_votes,
                   t.detection_count, t.duration_s
            FROM tracks t
            JOIN cameras c ON c.id = t.camera_id
            WHERE t.session_key = %s
        """, (session_key,))
        return [dict(r) for r in cur.fetchall()]


def bucket(value: float, edges: tuple[float, ...]) -> str:
    for lo, hi in zip(edges, edges[1:]):
        if lo <= value < hi:
            return f"{lo:.2f}–{hi:.2f}"
    return f"≥{edges[-1]:.2f}"


def score(tracks: list[dict], truth: dict[str, dict[str, dict]]) -> dict:
    """Compare every track that has a known intended plate against its consensus."""
    matched = 0
    exact = 0
    near = 0                       # confusion-aware similarity ≥ 0.9
    pred_chars = 0
    pred_err = 0
    single_total = 0               # every individual read, across all tracks
    single_exact = 0
    single_err = 0
    single_chars = 0
    by_reads: dict[int, list[int]] = defaultdict(list)      # read count → [hit, …]
    by_conf: dict[str, list[int]] = defaultdict(list)
    by_method: dict[str, list[int]] = defaultdict(list)
    unmatched_tracks = 0
    missing_consensus = 0
    failures: list[dict] = []

    for t in tracks:
        cam_truth = truth.get(t["external_id"]) or {}
        # JSON object keys are strings; track_id is an int in the DB.
        entry = cam_truth.get(str(t["track_id"]))
        if not entry or not entry.get("plate"):
            unmatched_tracks += 1
            continue
        want = entry["plate"]
        matched += 1

        votes = t.get("plate_votes") or {}
        raw = votes.get("raw") or []
        for r in raw:
            single_total += 1
            single_chars += len(want)
            d = char_errors(r, want)
            single_err += d
            if d == 0:
                single_exact += 1

        got = t.get("plate_text")
        if not got:
            missing_consensus += 1
            continue

        hit = int(got == want)
        exact += hit
        sim = plate_similarity(got, want)
        near += int(sim >= 0.90)
        pred_chars += len(want)
        pred_err += char_errors(got, want)

        n = int(t.get("plate_read_count") or len(raw) or 0)
        by_reads[min(n, 10)].append(hit)
        by_conf[bucket(float(t.get("plate_confidence") or 0.0),
                       (0.0, 0.2, 0.4, 0.6, 0.8, 1.0))].append(hit)
        by_method[str(votes.get("snap_method") or votes.get("method") or "?")].append(hit)

        if not hit:
            failures.append({
                "camera": t["external_id"], "track": t["track_id"],
                "want": want, "got": got, "sim": round(sim, 3),
                "reads": n, "conf": t.get("plate_confidence"),
                "raw": raw[:6],
            })

    return {
        "matched": matched, "exact": exact, "near": near,
        "missing_consensus": missing_consensus, "unmatched_tracks": unmatched_tracks,
        "pred_cer": (pred_err / pred_chars) if pred_chars else 0.0,
        "single_total": single_total, "single_exact": single_exact,
        "single_cer": (single_err / single_chars) if single_chars else 0.0,
        "by_reads": by_reads, "by_conf": by_conf, "by_method": by_method,
        "failures": failures,
    }


BAR = "─" * 68


def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:5.1f}%" if b else "  n/a"


def print_report(s: dict, show_failures: int) -> None:
    m, single = s["matched"], s["single_total"]
    print(BAR)
    print(f"Scored {m} tracks with a known intended plate "
          f"({s['unmatched_tracks']} tracks had none — background traffic without "
          f"a plated class, or no read at all)")
    print(BAR)
    print(f"  single-read exact      {pct(s['single_exact'], single)}"
          f"   ({s['single_exact']}/{single} individual frame reads)")
    print(f"  consensus exact        {pct(s['exact'], m)}"
          f"   ({s['exact']}/{m} tracks)")
    print(f"  consensus within 0.90  {pct(s['near'], m)}"
          f"   (confusion-aware similarity — a near miss an operator could still use)")
    if single and s["single_exact"]:
        lift = (s["exact"] / m) / (s["single_exact"] / single)
        print(f"  lift                   {lift:5.2f}×  over reading one frame")
    print(f"  character error rate   {s['single_cer']:.3f} per read "
          f"→ {s['pred_cer']:.3f} after the vote")
    if s["missing_consensus"]:
        print(f"  no consensus at all    {s['missing_consensus']} tracks had reads but "
              f"produced no plate")

    print(f"\n  Accuracy by number of reads — the claim that more frames help, tested:")
    for n in sorted(s["by_reads"]):
        hits = s["by_reads"][n]
        label = f"{n}" if n < 10 else "10+"
        print(f"    {label:>3s} reads  {pct(sum(hits), len(hits))}  (n={len(hits)})")

    print(f"\n  Accuracy by reported confidence — is the number calibrated?")
    for key in sorted(s["by_conf"]):
        hits = s["by_conf"][key]
        print(f"    {key:>10s}  {pct(sum(hits), len(hits))}  (n={len(hits)})")

    print(f"\n  Accuracy by how the plate was resolved:")
    for key, hits in sorted(s["by_method"].items(), key=lambda kv: -len(kv[1])):
        print(f"    {key:>16s}  {pct(sum(hits), len(hits))}  (n={len(hits)})")

    if show_failures and s["failures"]:
        print(f"\n  Worst misses (by similarity):")
        for f in sorted(s["failures"], key=lambda f: f["sim"])[:show_failures]:
            print(f"    {f['camera']} #{f['track']}  want {f['want']:<11s} got {f['got']:<11s}"
                  f" sim {f['sim']:.2f}  {f['reads']} reads  conf {f['conf']}")
            print(f"        raw: {' '.join(f['raw'])}")
    print(BAR)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score ANPR consensus against seeded ground truth")
    ap.add_argument("--session-key", default="demo-7")
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--show-failures", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="machine-readable summary only")
    args = ap.parse_args()

    truth = load_truth(args.report)
    conn = get_connection()
    tracks = fetch_tracks(conn, args.session_key)
    if not tracks:
        sys.exit(f"No tracks for session_key={args.session_key!r}. "
                 f"Run: python -m ml.analytics.runner --session-key {args.session_key}")
    s = score(tracks, truth)

    if args.json:
        m, single = s["matched"], s["single_total"]
        print(json.dumps({
            "session_key": args.session_key,
            "tracks_scored": m,
            "single_read_exact": round(s["single_exact"] / single, 4) if single else None,
            "consensus_exact": round(s["exact"] / m, 4) if m else None,
            "consensus_near_0_90": round(s["near"] / m, 4) if m else None,
            "cer_single": round(s["single_cer"], 4),
            "cer_consensus": round(s["pred_cer"], 4),
        }, indent=2))
        return
    print_report(s, args.show_failures)


if __name__ == "__main__":
    main()
