"""Score the plate pipeline against every plate a human has read.

Reads both ground-truth sets — the original nine and the extended sheet pass —
and reports what the system's *current* database says, not what it said when a
set was labelled. That matters: a label file that caches the prediction goes
stale the moment the model changes, and a stale comparison is worse than none.

Three numbers, kept apart because they are three different failures:

    exact match        of the plates a human could read, how many the system
                       got character-for-character right
    character error    how wrong the wrong ones are — a one-character miss and
                       a complete miss are not the same operational problem
    detector precision how often the thing the detector boxed was a plate at
                       all, rather than a bus destination board or an operator's
                       livery

And the calibration table, because a confidence that does not predict
correctness is decoration.

Usage:
    python -m scripts.score_plate_truth
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import psycopg2

from ml import rto
from ml.plate_trust import plate_trust
from ml.plate_validator import is_probably_truncated, matches_format

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
TRUTH_FILES = ["artifacts/plate_truth.json", "artifacts/plate_truth_ext.json"]


def lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j - 1] + (ca != cb), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show-misses", type=int, default=12)
    args = ap.parse_args()

    rows: list[dict] = []
    for path in TRUTH_FILES + sorted(glob.glob("artifacts/plate_truth_*.json")):
        if not os.path.exists(path) or path.endswith("_reads.json"):
            continue
        with open(path, encoding="utf-8") as fh:
            for key, r in json.load(fh).items():
                r = dict(r)
                tag = os.path.basename(path).replace("plate_truth", "")
                r["_key"] = f"{tag.strip('_.json') or 'base'}#{key}"
                rows.append(r)
    # A track labelled in both passes is one sample, not two.
    by_track: dict[str, dict] = {}
    for r in rows:
        by_track.setdefault(r["track_id"], r)
    rows = list(by_track.values())
    if not rows:
        raise SystemExit("No ground-truth files. Run scripts/export_plate_sheets.py first.")

    cur = psycopg2.connect(DB).cursor()
    legible = [r for r in rows if (r.get("truth") or "").strip()]
    not_plate = [r for r in rows if r.get("is_plate") is False]
    unreadable = [r for r in rows if r.get("is_plate") is not False
                  and not (r.get("truth") or "").strip()]

    ok = ce = chars = 0
    bands: dict[str, list[int]] = {}
    misses: list[tuple] = []

    for r in legible:
        t = r["truth"].strip().upper()
        cur.execute("SELECT plate_text, plate_confidence, plate_read_count "
                    "FROM tracks WHERE id = %s", (r["track_id"],))
        row = cur.fetchone()
        got = (row[0] or "").upper() if row else ""
        conf = (row[1] or 0.0) if row else 0.0
        reads = (row[2] or 0) if row else 0
        tier = plate_trust(conf, read_count=reads,
                           truncated=is_probably_truncated(got)).tier if got else "none"
        hit = got == t
        ok += hit
        chars += len(t)
        ce += lev(got, t)
        bands.setdefault(tier, [0, 0])
        bands[tier][0] += 1
        bands[tier][1] += hit
        if not hit:
            misses.append((r.get("camera", "?"), t, got or "—", conf, reads, tier))

    n = len(legible)
    boxed = len(legible) + len(not_plate) + len(unreadable)
    print(f"{'':=<64}")
    print("  ANPR measured against plates read by a human")
    print(f"{'':=<64}")
    print(f"  plates a human could read   {n}")
    print(f"  exact match                 {ok}/{n}   ({100 * ok / max(n,1):.0f}%)")
    print(f"  character error rate        {100 * ce / max(chars,1):.1f}%")
    print()
    print(f"  regions the detector boxed  {boxed}")
    print(f"    a readable plate          {n}")
    print(f"    a plate, too degraded     {len(unreadable)}")
    print(f"    not a plate at all        {len(not_plate)}"
          f"   ← detector precision {100 * (boxed - len(not_plate)) / max(boxed,1):.0f}%")
    print()
    print("  calibration — does confidence predict correctness?")
    print(f"    {'tier':<12}{'n':>4}{'exact':>7}{'rate':>8}")
    for tier in ("actionable", "verify", "lead", "none"):
        if tier in bands:
            tot, hits = bands[tier]
            print(f"    {tier:<12}{tot:>4}{hits:>7}{100 * hits / tot:>7.0f}%")

    if misses and args.show_misses:
        print("\n  misses")
        for cam, t, got, conf, reads, tier in misses[:args.show_misses]:
            print(f"    {cam:<7} truth {t:<12} read {got:<12} "
                  f"conf {conf:.2f}  reads {reads}  {tier}")

    # ── Estate-wide: is what we present even a registration? ────────────────
    # This is a *consistency* measure, not an accuracy one, and the difference
    # matters. It cannot tell a correct plate from a legal-looking wrong one —
    # only the hand-read set above can do that. What it can do is cover all
    # 2,000 tracks instead of fifteen, and catch the failure the hand-read set
    # structurally cannot see: a string that is not a plate at all being
    # presented to an officer as a vehicle's identity. Thirty-four of these
    # were live in this estate, twenty of them the OCR's own misspelled word
    # UNREADABLE.
    cur.execute("SELECT plate_text FROM tracks WHERE plate_text IS NOT NULL")
    presented = [r[0] for r in cur.fetchall()]
    illegal = [p for p in presented if not matches_format(p)]
    no_rto = [p for p in presented if not rto.is_known_rto(p)]
    print()
    print(f"{'':-<64}")
    print(f"  estate-wide, every track shown with a plate   {len(presented)}")
    print(f"    a legal Indian registration                 "
          f"{len(presented) - len(illegal)}"
          f"  ({100 * (len(presented) - len(illegal)) / max(len(presented),1):.0f}%)")
    print(f"    naming an RTO that has been issued          "
          f"{len(presented) - len(no_rto)}"
          f"  ({100 * (len(presented) - len(no_rto)) / max(len(presented),1):.0f}%)")
    if illegal:
        print(f"    not a registration                          {len(illegal)}"
              f"   {', '.join(sorted(set(illegal))[:6])}")


if __name__ == "__main__":
    main()
