"""Print the per-character vote behind a plate read.

The console shows this panel too (Search → the plate → Investigate). This exists
for the times a terminal is the right surface: a demo where clicking through
costs more than it explains, a figure for the design document, or checking a
read without a browser.

    python -m scripts.show_plate_votes GJ23CF3136
    python -m scripts.show_plate_votes GJ23CF3136 --all

What it prints, and why each part is there:

  * one column per plate position, the winning character with its accumulated
    weight, and the runners-up under it;
  * a contested column marked, on the same rule the console uses — the
    runner-up carried more than a third of the winner's weight;
  * the raw single-frame reads that were voted over, because the point of the
    panel is that no single frame decided the answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

# Same rule as components/Evidence.tsx, so the two surfaces never disagree about
# which column is contested.
CONTESTED = 0.33


def rows_for(conn, plate: str, every: bool):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.plate_text, t.plate_read_count, t.plate_confidence,
                   t.plate_votes, t.first_seen, c.external_id AS camera
            FROM tracks t JOIN cameras c ON c.id = t.camera_id
            WHERE upper(t.plate_text) = upper(%s)
              AND t.plate_votes ? 'positions'
            ORDER BY t.plate_read_count DESC
            """,
            (plate,),
        )
        found = cur.fetchall()
    return found if every else found[:1]


def show(row) -> None:
    v = row["plate_votes"]
    if isinstance(v, str):
        v = json.loads(v)
    positions = v.get("positions", [])

    conf = row["plate_confidence"] or 0.0
    print(f"\n  {row['plate_text']}   {row['plate_read_count']} reads   "
          f"confidence {conf:.2f}   {row['camera']}   {row['first_seen']:%Y-%m-%d %H:%M:%S}")
    print(f"  method {v.get('method')}   mean margin {v.get('mean_margin')}   "
          f"RTO known: {v.get('rto_known')}   format valid: {v.get('format_valid')}")

    win, wgt, alt, mark = [], [], [], []
    for p in positions:
        top = p.get("top") or []
        w = top[0][1] if top else 0.0
        r = top[1][1] if len(top) > 1 else 0.0
        win.append(f"{p['winner']:^7}")
        wgt.append(f"{w:^7.1f}")
        alt.append(f"{(top[1][0] + ' ' + format(r, '.1f')) if len(top) > 1 else '-':^7}")
        mark.append(f"{('^^^' if w and r / w > CONTESTED else ''):^7}")

    print("\n  position  " + "".join(f"{p['pos']:^7}" for p in positions))
    print("  winner    " + "".join(win))
    print("  weight    " + "".join(wgt))
    print("  runner-up " + "".join(alt))
    print("  contested " + "".join(mark))

    raw = v.get("raw") or []
    print(f"\n  the {len(raw)} single-frame reads voted over:")
    # Grouped, because the interesting fact is usually that they disagreed.
    tally: dict[str, int] = {}
    for r in raw:
        tally[r] = tally.get(r, 0) + 1
    for text, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        flag = "" if text == row["plate_text"] else "   <- outvoted"
        print(f"    {n:>3} x  {text}{flag}")
    if v.get("abstained"):
        print(f"    {v['abstained']} read(s) abstained and were dropped before voting")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plate", help="registration to look up, e.g. GJ23CF3136")
    ap.add_argument("--all", action="store_true",
                    help="every track carrying this plate, not just the best-supported")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("  DATABASE_URL is not set. Source the project's .env first.")

    conn = psycopg2.connect(url)
    try:
        found = rows_for(conn, args.plate, args.all)
    finally:
        conn.close()

    if not found:
        # A plate with no vote record is a real state, not an error: single-read
        # tracks carry no ballot to show.
        raise SystemExit(f"  No track carries {args.plate.upper()} with a recorded vote.")
    for row in found:
        show(row)
    print()


if __name__ == "__main__":
    main()
