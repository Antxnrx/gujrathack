"""Classify each camera by whether ANPR can work there, from measured yield.

Why this is the right answer, and a fine-tune is not
----------------------------------------------------
The plate detector was the obvious suspect for the 94% of crops that produce no
box. It is not. Harvesting candidate boxes from three detectors at 0.05
confidence over 6,000 crops produced 2,521 candidates, of which 24 read as a
legal plate — and **every one of those 24 was found by more than one detector**.
There was no pool of plates that one model sees and another misses, which is the
only thing a fine-tune could have exploited.

What there is instead is geometry, and it is stark:

    vehicle width   crops    reads    rate
    < 60 px        10,694        0   0.00%
    60-120 px      10,839        7   0.06%
    120-200 px      5,184      155   2.99%
    200-350 px      2,596      143   5.51%
    350+ px           974       59   6.06%

Zero reads from ten thousand crops under 60 px. Those plates are not being
missed; they were never sampled. 71% of this estate's vehicle crops sit below
120 px, which bounds ANPR far more tightly than any model choice.

So the useful thing to build is not a better detector but an honest capability
label. Each camera is scored on what it actually delivered, written to
``cameras.metadata.anpr``, and surfaced so an operator knows which cameras can
answer a plate question and which cannot. A system that says "this camera cannot
read plates" is worth more than one that silently returns nothing.

Usage:
    python -m scripts.score_camera_anpr            # score and write
    python -m scripts.score_camera_anpr --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging

import psycopg2
import psycopg2.extras

log = logging.getLogger("setu.anpr_score")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"
VEHICLES = ("car", "truck", "bus", "motorcycle", "auto_rickshaw", "tractor")

# Read rate at which a camera is worth pointing a plate question at. Derived from
# the distribution above rather than chosen: the estate's overall vehicle read
# rate is ~1.2%, and cameras clearing 3% are the ones whose geometry puts plates
# above the resolvability floor.
TIER_ANPR = 0.03
TIER_OCCASIONAL = 0.005


def tier(rate: float, median_w: float) -> str:
    if rate >= TIER_ANPR:
        return "anpr_capable"
    if rate >= TIER_OCCASIONAL:
        return "occasional"
    if median_w and median_w < 80:
        return "overview_only"
    return "no_plates_observed"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT c.id, c.external_id, c.name,
               COUNT(*)                                   AS vehicle_crops,
               COUNT(d.plate_text)                         AS plate_reads,
               PERCENTILE_CONT(0.5) WITHIN GROUP (
                   ORDER BY (d.bbox->>'x2')::float - (d.bbox->>'x1')::float
               )                                           AS median_width,
               PERCENTILE_CONT(0.9) WITHIN GROUP (
                   ORDER BY (d.bbox->>'x2')::float - (d.bbox->>'x1')::float
               )                                           AS p90_width
        FROM cameras c
        LEFT JOIN detections d ON d.camera_id = c.id AND d.class IN %s
        GROUP BY c.id, c.external_id, c.name
        ORDER BY c.external_id
        """,
        (VEHICLES,),
    )
    rows = cur.fetchall()

    write = conn.cursor()
    counts: dict[str, int] = {}
    print(
        f"{'camera':<8}{'crops':>8}{'reads':>7}{'rate':>8}"
        f"{'med w':>7}{'p90 w':>7}  {'tier':<20}{'name'}"
    )
    print("-" * 104)
    for r in rows:
        crops = r["vehicle_crops"] or 0
        reads = r["plate_reads"] or 0
        rate = reads / crops if crops else 0.0
        med = float(r["median_width"] or 0)
        t = tier(rate, med) if crops else "no_data"
        counts[t] = counts.get(t, 0) + 1

        payload = {
            "tier": t,
            "vehicle_crops": crops,
            "plate_reads": reads,
            "read_rate": round(rate, 5),
            "median_vehicle_width_px": round(med, 1),
            "p90_vehicle_width_px": round(float(r["p90_width"] or 0), 1),
            "measured_on": "restored GPU index, 2026-09-04/05",
        }
        if not args.dry_run:
            write.execute(
                "UPDATE cameras SET metadata = metadata || %s::jsonb WHERE id = %s",
                (json.dumps({"anpr": payload}), r["id"]),
            )
        print(f"{r['external_id']:<8}{crops:>8}{reads:>7}{rate*100:>7.2f}%"
              f"{med:>7.0f}{float(r['p90_width'] or 0):>7.0f}  {t:<20}{(r['name'] or '')[:34]}")

    if not args.dry_run:
        conn.commit()

    print("-" * 104)
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<22}{n:>3} cameras")
    print()
    print("Written to cameras.metadata.anpr" if not args.dry_run else "Dry run — nothing written")


if __name__ == "__main__":
    main()
