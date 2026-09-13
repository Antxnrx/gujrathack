"""Keep the fold running, so live detections become tracks and events.

Why this exists
---------------
``ml/worker.py`` writes ``detections``. ``ml/analytics/runner.py`` folds those
into ``tracks`` and runs the seven rules that produce ``events``. On the indexed
archive the runner was invoked by hand after each batch, which was fine —
nothing was arriving in between.

Nothing invoked it on a schedule. Its own docstring offers ``--since 30m``
"for a cron every 5 minutes", and no cron, compose service or run.sh line ever
called it. On live footage that is not a missing nicety, it is the system
quietly failing: detections accumulate, and

  * no track is ever built, so no plate gets its multi-frame consensus —
    the single-frame read is all there would be;
  * no rule ever fires, so loitering, zone intrusion, wrong-way, stopped
    vehicle and crowd produce nothing;
  * the timeline stays empty and the dashboard counters stay flat while the
    database fills up.

An operator would watch a working wall of cameras and a console that never said
anything, with no error anywhere to explain it.

The lookback, and why it is generous
------------------------------------
``build_tracks(since=…)`` filters *detections*, so a track straddling the
boundary is rebuilt from its visible part only — and since the upsert now takes
``EXCLUDED`` for ``first_seen``/``last_seen`` (see ml/analytics/tracks.py), a
short lookback would not merely miss the earlier frames, it would rewrite the
track's window to start at the boundary. A vehicle that entered two minutes ago
would be recorded as having appeared just now.

So the lookback is sized against the longest track this estate actually
produces. After splitting on time gaps the longest is 118 seconds and the mean
2.7. Twenty minutes is roughly ten times the longest, which costs a little
repeated work each pass and removes the failure mode entirely. The repeated work
is bounded and idempotent: track upserts key on
``(camera, track_id, session)`` and event inserts on a deterministic dedupe key,
so a second pass over the same window updates in place and adds nothing.

Running it
----------
No GPU, no camera access, no Sentinel credential — only ``DATABASE_URL``. It is
a separate process on purpose, so the fold keeps running when the inference
worker is restarted, and so it can be moved to its own box without moving
anything else.

    python -m ml.analytics.service                    # every 60s, 20m lookback
    python -m ml.analytics.service --interval 30
    python -m ml.analytics.service --once             # one pass, for a cron
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta

log = logging.getLogger("setu.analytics.service")

# How far back each pass reaches. See the module docstring: this is sized
# against the longest track the estate produces, not against the interval.
DEFAULT_LOOKBACK_MINUTES = 20
DEFAULT_INTERVAL_SECONDS = 60

_stop = threading.Event()


def _one_pass(lookback_minutes: int, quiet_when_idle: bool = True) -> dict:
    """Fold, then run the rules, over the recent window."""
    from ml.analytics.rules import run_rules
    from ml.analytics.tracks import build_tracks
    from ml.db import get_connection

    since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    conn = get_connection()
    try:
        fold = build_tracks(conn, since=since)
        rules = run_rules(conn, since=since)
        new_events = sum(r.get("new", 0) for r in rules["rules"].values())

        # A live system logs every minute forever. Saying "0 new" sixty times an
        # hour trains whoever is watching the logs to stop reading them, so a
        # quiet pass stays quiet and only the first idle pass says so.
        if fold["tracks"] or new_events or not quiet_when_idle:
            log.info("%d track(s) folded from %d detection(s) · %d new event(s)",
                     fold["tracks"], fold["detections"], new_events)
        return {"tracks": fold["tracks"], "detections": fold["detections"],
                "events": new_events, "plates": fold.get("plates", 0)}
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                    help="seconds between passes (default 60)")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_MINUTES,
                    help="minutes of detections each pass reconsiders (default 20)")
    ap.add_argument("--once", action="store_true", help="one pass and exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every pass, including the ones that did nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.lookback * 60 < args.interval:
        raise SystemExit(
            f"  --lookback ({args.lookback}m) is shorter than --interval "
            f"({args.interval}s). Detections arriving between passes would never "
            f"be folded at all. Raise the lookback."
        )

    if args.once:
        _one_pass(args.lookback, quiet_when_idle=False)
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _stop.set())

    log.info("Analytics service up — every %ds over the last %dm. Ctrl-C to stop.",
             args.interval, args.lookback)
    idle_reported = False
    while not _stop.is_set():
        started = time.monotonic()
        try:
            out = _one_pass(args.lookback, quiet_when_idle=not args.verbose)
            if out["tracks"] or out["events"]:
                idle_reported = False
            elif not idle_reported:
                log.info("Nothing new to fold. Staying quiet until something arrives.")
                idle_reported = True
        except Exception:
            # A pass failing must not end the service: the usual causes are
            # transient (the database restarting, a lock timeout) and the next
            # pass reconsiders the same window anyway, so recovery is automatic.
            log.exception("Analytics pass failed — retrying at the next interval")

        # Sleep the remainder, so a slow pass does not drift the schedule.
        _stop.wait(max(1.0, args.interval - (time.monotonic() - started)))

    log.info("Analytics service stopped.")


if __name__ == "__main__":
    main()
