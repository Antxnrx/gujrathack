"""Re-grade every stored plate against the registration grammar.

Why this has to be a backfill and not just a forward fix
-------------------------------------------------------
The pipeline used to return an unvalidated OCR read when the grammar rejected
it, on the reasoning that the track-level vote is the stage designed to recover
a broken read and discarding it early would starve that stage. That reasoning is
right about the *vote* and wrong about the *database*: when the vote also failed,
the raw string was written to ``tracks.plate_text`` anyway and shown to
operators as a vehicle's identity.

What that produced here, on real footage:

    34 of 111 tracks   carried a "registration" that is not one
    20 of those        were the word UNREADABLE, misspelled — the OCR's own
                       admission that it could not read the crop, stored as
                       though it were a number plate
    the rest           KH2222, UNRD71, M062026, TA0AAKS10, DPTC780

An officer looking at that screen cannot tell those from a real read. That is
the failure mode worth the most here, because it is not "the system is wrong",
it is "the system is confidently wrong in a way the user cannot detect".

What this does
--------------
Every stored string — track and detection alike — is re-validated where it
stands. It does *not* re-fold tracks from their frame reads, which was the first
attempt and was wrong twice over:

  * it discards ``refine_track_plates``' work, and the best-crop read is the
    most accurate answer this system produces (measured: best crop 78%,
    highest-confidence frame 67%, consensus 56% on the same plates);
  * the fold will snap noise onto a legal plate if any lands inside its budget,
    so tracks whose frames all read UNREADABLE came back as ``KA02N1222`` and
    ``KA03YL0`` — inventing a Karnataka registration out of an admission of
    defeat is strictly worse than the blank it replaced.

Re-reading a crop is a job for ``scripts/refine_track_plates.py``, which now
runs the grammar too. Keeping the two separate means each mechanism is judged on
its own evidence.

Nothing is deleted. A rejected read moves into ``plate_votes.rejected_raw``, so
the track still records what the camera saw; only the column an officer acts on
is cleared. An empty plate is a true statement about this vehicle, and a wrong
one is not.

Usage:
    python -m scripts.regrade_plates --dry-run
    python -m scripts.regrade_plates
"""
from __future__ import annotations

import argparse
import json
import logging

import psycopg2
import psycopg2.extras

from ml import rto
from ml.plate_validator import validate_plate

log = logging.getLogger("setu.regrade")

DB = "postgresql://setu:setu_dev_only@localhost:5434/setu"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    write = conn.cursor()

    # ── Detections: each is a single frame's read, so validate it as one ─────
    cur.execute("SELECT id, plate_text FROM detections WHERE plate_text IS NOT NULL")
    dets = cur.fetchall()
    d_cleared = d_changed = d_kept = 0
    for d in dets:
        fixed = validate_plate(d["plate_text"])
        if fixed == d["plate_text"]:
            d_kept += 1
            continue
        if not args.dry_run:
            write.execute("UPDATE detections SET plate_text=%s WHERE id=%s", (fixed, d["id"]))
        if fixed is None:
            d_cleared += 1
        else:
            d_changed += 1

    # ── Tracks: re-validate the string the operator actually sees ───────────
    cur.execute(
        """SELECT t.id, t.plate_text, t.plate_confidence, t.plate_read_count, t.plate_votes,
                  c.external_id AS cam
           FROM tracks t JOIN cameras c ON c.id = t.camera_id
           WHERE t.plate_text IS NOT NULL"""
    )
    tracks = cur.fetchall()

    cleared: list[tuple] = []
    changed: list[tuple] = []
    kept = 0

    for t in tracks:
        was = t["plate_text"]
        now = validate_plate(was)
        conf = float(t["plate_confidence"] or 0) if now else None
        votes = {**(t["plate_votes"] or {}), "regraded": True}

        if now == was:
            kept += 1
            continue

        if now is None:
            votes["rejected_raw"] = was
            votes["rejected_reason"] = ("ocr_abstention" if rto.is_abstention(was)
                                        else "not_a_registration")
            cleared.append((t["cam"], was, votes["rejected_reason"]))
        else:
            votes["regraded_from"] = was
            changed.append((t["cam"], was, now))

        if not args.dry_run:
            write.execute(
                "UPDATE tracks SET plate_text=%s, plate_confidence=%s, "
                "plate_votes=%s::jsonb WHERE id=%s",
                (now, conf, json.dumps(votes), t["id"]),
            )

    if not args.dry_run:
        conn.commit()

    # ── What the estate looks like afterwards ───────────────────────────────
    # Computed from the outcome rather than re-read from the table, so that a
    # dry run describes what *would* happen instead of reporting the state it
    # deliberately did not change.
    after = [t["plate_text"] for t in tracks
             if validate_plate(t["plate_text"]) is not None]
    after = [validate_plate(p) for p in after]
    unknown_rto = [p for p in after if not rto.is_known_rto(p)]

    print()
    print(f"{'':=<70}")
    print("  Re-graded against the registration grammar"
          + ("   [DRY RUN]" if args.dry_run else ""))
    print(f"{'':=<70}")
    print(f"  detections   {len(dets):5} with a plate   "
          f"{d_kept} unchanged · {d_changed} corrected · {d_cleared} cleared")
    print(f"  tracks       {len(tracks):5} with a plate   "
          f"{kept} unchanged · {len(changed)} corrected · {len(cleared)} cleared")
    print()
    print(f"  tracks still carrying a plate  {len(after)}")
    print(f"    of those, an RTO that exists {len(after) - len(unknown_rto)}")
    if unknown_rto:
        print(f"    an RTO Gujarat never issued  {len(unknown_rto)}  "
              f"{', '.join(sorted(set(unknown_rto))[:6])}")

    if changed:
        print(f"\n  corrected by the template ({len(changed)})")
        print(f"    {'cam':<8}{'was':<14}{'now':<14}district")
        for cam, was, now in changed[:args.show]:
            print(f"    {cam:<8}{was:<14}{now:<14}{rto.district(now) or ''}")
    if cleared:
        print(f"\n  cleared — not a registration ({len(cleared)})")
        print(f"    {'cam':<8}{'stored as':<14}why")
        for cam, was, why in cleared[:args.show]:
            print(f"    {cam:<8}{was:<14}{why}")
        print("\n  These rows keep the raw string in plate_votes.rejected_raw.")
        print("  The plate column is empty because empty is what is true.")


if __name__ == "__main__":
    main()
