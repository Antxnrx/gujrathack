"""Match a watchlist against footage already indexed — "has this vehicle been seen?"

Why this is a capability, not a demo fixture
--------------------------------------------
``ml/watchlist.py`` checks each detection as the worker produces it, which
answers "tell me when this plate appears next". That is half the question an
investigator actually has. The other half — *has it appeared already* — has no
answer at all today, because a plate added to the watchlist at noon is never
compared against the morning's footage. On this estate that is 62,000 detections
the system has looked at and cannot be asked about.

So this scans the index. Adding a plate and immediately learning it passed
cam09 at 06:12 yesterday is, for a police user, the more useful direction of the
two.

Matching is confusion-aware, not exact
--------------------------------------
An exact string match is the wrong test when the reader is 78% accurate. A
vehicle recorded as ``GJ01RN7172`` may sit in the index as ``GJ01RN717`` or
``GJ0IRN7172`` — one dropped character, one O/0 swap. ``plate_similarity`` in
``ml/analytics/anpr.py`` already scores those at ~0.95 because it costs a
confusion-class substitution a quarter of a normal one, so it is the right
comparison here and the threshold is exposed rather than buried.

Every hit records *how* it matched, so an operator can see the difference
between an exact hit and a one-character inference before acting on it.

Usage:
    python -m scripts.watchlist_scan --add GJ11BR2912 --reason "Stolen vehicle — FIR 114/2026"
    python -m scripts.watchlist_scan                      # scan everything on the list
    python -m scripts.watchlist_scan --threshold 0.92
"""
from __future__ import annotations

import argparse
import logging

import psycopg2
import psycopg2.extras

from ml.analytics.anpr import plate_similarity

log = logging.getLogger("setu.watchlist")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"

# Below this two plates are different vehicles. 0.90 admits one confusion-class
# substitution in a ten-character plate (O for 0, I for 1) and rejects two
# unrelated registrations, which is the line that matters: a false hit sends a
# unit to the wrong vehicle.
DEFAULT_THRESHOLD = 0.90


def add_entry(conn, plate: str, reason: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM watchlist_entries WHERE type='plate' AND upper(value)=upper(%s)",
            (plate,),
        )
        row = cur.fetchone()
        if row:
            log.info("%s is already on the watchlist", plate)
            return str(row[0])
        cur.execute(
            "INSERT INTO watchlist_entries (type, value, reason) VALUES ('plate', %s, %s) "
            "RETURNING id",
            (plate.upper(), reason),
        )
        conn.commit()
        return str(cur.fetchone()[0])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--add", help="add this plate to the watchlist before scanning")
    ap.add_argument("--reason", default="Added for retrospective scan")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    conn = psycopg2.connect(DB)
    if args.add:
        entry_id = add_entry(conn, args.add, args.reason)
        log.info("Watchlist entry %s for %s", entry_id[:8], args.add.upper())

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, value, reason FROM watchlist_entries WHERE type='plate' "
                "AND value IS NOT NULL AND value <> ''")
    entries = cur.fetchall()
    if not entries:
        raise SystemExit("No plate entries on the watchlist. Add one with --add.")

    # Every plate the index holds, with where and when it was seen.
    cur.execute(
        """SELECT d.id AS detection_id, d.plate_text, d.plate_confidence, d.ts,
                  d.track_uuid, c.external_id AS camera, c.name AS camera_name
           FROM detections d JOIN cameras c ON c.id = d.camera_id
           WHERE d.plate_text IS NOT NULL"""
    )
    sightings = cur.fetchall()
    log.info("Scanning %d indexed plate reads against %d watchlist entries",
             len(sightings), len(entries))

    write = conn.cursor()
    created = skipped = collapsed = 0
    report: list[tuple] = []

    for entry in entries:
        wanted = (entry["value"] or "").upper()

        # One alert per *vehicle pass*, not per frame.
        #
        # A vehicle crossing a camera is read on every frame it is visible for,
        # so a single pass of GJ11BR2912 past cam06 produced ten identical
        # matches a second apart. Ten rows in the alert queue for one car is not
        # ten pieces of information — it is one, repeated until the operator
        # stops reading the queue. The track is the vehicle, so the best read on
        # each track is the alert, and the rest are its evidence.
        best_per_track: dict[str, dict] = {}
        for s in sightings:
            sim = plate_similarity(wanted, s["plate_text"])
            if sim < args.threshold:
                continue
            # Detections with no track still deserve an alert; key them on
            # themselves so they are never collapsed into someone else's pass.
            key = str(s["track_uuid"]) if s["track_uuid"] else f"det:{s['detection_id']}"
            prev = best_per_track.get(key)
            if prev is None or (s["plate_confidence"] or 0) > (prev["plate_confidence"] or 0):
                best_per_track[key] = {**s, "_sim": sim}
            if prev is not None:
                collapsed += 1

        for s in best_per_track.values():
            how = "exact" if (s["plate_text"] or "").upper() == wanted else f"fuzzy {s['_sim']:.2f}"

            # Re-running a scan must not produce a second copy of a hit an
            # operator has already triaged.
            write.execute(
                "SELECT 1 FROM alerts WHERE watchlist_entry_id=%s AND detection_id=%s",
                (entry["id"], s["detection_id"]),
            )
            if write.fetchone():
                skipped += 1
                continue

            if not args.dry_run:
                write.execute(
                    "INSERT INTO alerts (watchlist_entry_id, detection_id, alert_type, status) "
                    "VALUES (%s, %s, 'watchlist_match', 'pending')",
                    (entry["id"], s["detection_id"]),
                )
            created += 1
            report.append((wanted, s["plate_text"], how, s["camera"],
                           s["camera_name"], s["ts"]))

    if not args.dry_run:
        conn.commit()

    print()
    print(f"{'':-<78}")
    print(f"  watchlist entries scanned  {len(entries)}")
    print(f"  indexed plate reads        {len(sightings)}")
    print(f"  alerts raised              {created}")
    print(f"  already known, skipped     {skipped}")
    print(f"  frames collapsed into a pass {collapsed}")
    print(f"{'':-<78}")
    if report:
        print(f"\n  {'wanted':<12}{'seen as':<12}{'match':<12}{'camera':<8}{'when'}")
        for wanted, seen, how, cam, cam_name, ts in report[:20]:
            print(f"  {wanted:<12}{(seen or ''):<12}{how:<12}{cam:<8}"
                  f"{ts:%d %b %H:%M:%S}  {(cam_name or '')[:28]}")
    print("\n  Each insert fires pg_notify('setu_alerts'), so an open console"
          "\n  receives these over SSE without polling.")


if __name__ == "__main__":
    main()
