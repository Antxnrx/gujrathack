"""CLI for the analytics layer: fold detections into tracks, then run the rules.

This is the step between inference and the operator. ``ml/worker.py`` and
``scripts/batch_index.py`` fill ``detections``; this fills ``tracks`` and
``events``. It needs no GPU, no camera access and no Sentinel credentials — only
``DATABASE_URL`` — so it can run on a cron box, in a container beside the API, or
by hand after a batch index.

Usage::

    # after a batch index — fold everything, then run every rule
    python -m ml.analytics.runner

    # one camera, tracks only
    python -m ml.analytics.runner --camera cam04 --skip-rules

    # tune a threshold and see what *would* fire, writing nothing
    python -m ml.analytics.runner --rules loitering --dry-run \\
        --param loiter_min_dwell_s=45

    # incremental: only the last 30 minutes (for a cron every 5 minutes)
    python -m ml.analytics.runner --since 30m

Safe to re-run. Track upserts are keyed on ``(camera, track_id, session)`` and
event inserts on a deterministic ``dedupe_key``, so a second pass over the same
footage updates tracks in place and adds no duplicate events — and never
resurrects an event an operator has already dismissed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from ml.analytics.rules import RULES, RuleParams, run_rules
from ml.analytics.tracks import build_tracks

log = logging.getLogger("setu.analytics")

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_since(value: str | None) -> datetime | None:
    """``30m`` / ``6h`` / ``2d`` → an absolute UTC timestamp. ISO also accepted."""
    if not value:
        return None
    m = _DURATION.match(value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: n})
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_params(pairs: list[str]) -> dict:
    """``--param key=value`` pairs into a dict, numbers coerced.

    Validated against :class:`RuleParams` fields here rather than silently
    ignored, because a typo in a threshold name during tuning is otherwise
    invisible: the run succeeds and the threshold you thought you changed didn't.
    """
    known = set(RuleParams().__dataclass_fields__)
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in known:
            raise SystemExit(
                f"unknown parameter {key!r}. Known: {', '.join(sorted(known))}"
            )
        try:
            out[key] = int(raw) if re.fullmatch(r"-?\d+", raw.strip()) else float(raw)
        except ValueError:
            raise SystemExit(f"--param {key} must be numeric, got {raw!r}")
    return out


def resolve_camera(conn, ident: str | None) -> str | None:
    """Accept either a camera UUID or a Sentinel external id (``cam04``)."""
    if not ident:
        return None
    try:
        from uuid import UUID
        UUID(ident)
        return ident
    except ValueError:
        pass
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM cameras WHERE external_id = %s", (ident,))
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"no camera with external_id {ident!r}")
    return str(row["id"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Setu analytics: fold detections into tracks, run event rules",
    )
    parser.add_argument("--camera", default=None,
                        help="Camera UUID or external id (cam04). Default: all")
    parser.add_argument("--since", default=None, metavar="30m|6h|ISO",
                        help="Only consider recent rows (default: everything)")
    parser.add_argument("--session-key", default=None,
                        help="Restrict to one ingest run's tracks")
    parser.add_argument("--skip-tracks", action="store_true",
                        help="Don't rebuild tracks, only run rules over existing ones")
    parser.add_argument("--skip-rules", action="store_true",
                        help="Only rebuild tracks, don't run rules")
    parser.add_argument("--rules", default=None,
                        help=f"Comma-separated subset of: {','.join(RULES)}")
    parser.add_argument("--param", action="append", metavar="key=value",
                        help="Override a RuleParams threshold (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what the rules would emit; write nothing")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON on stdout (for cron/CI)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    since = parse_since(args.since)
    overrides = parse_params(args.param)
    params = RuleParams().merged(overrides) if overrides else RuleParams()
    rule_names = [r.strip() for r in args.rules.split(",")] if args.rules else None

    # Imported here, not at module scope: ml.db pulls in ml.config, which
    # requires SENTINEL_EMAIL. Nothing in this module needs a camera credential,
    # and failing at import time over one would be a lie about the dependency.
    from ml.db import get_connection

    report: dict = {"started_at": datetime.now(timezone.utc).isoformat()}
    t0 = time.monotonic()
    conn = get_connection()
    try:
        camera_id = resolve_camera(conn, args.camera)

        if not args.skip_tracks:
            log.info("Folding detections into tracks%s%s…",
                     f" for camera {args.camera}" if args.camera else "",
                     f" since {since.isoformat()}" if since else "")
            fold = build_tracks(conn, camera_id=camera_id, since=since,
                                session_key=args.session_key)
            report["tracks"] = fold
            log.info("Tracks: %d built from %d detections (%d with a consensus "
                     "plate, %d too short to be meaningful)",
                     fold["tracks"], fold["detections"], fold["plates"],
                     fold["skipped_short"])
        else:
            log.info("--skip-tracks: using existing tracks")

        if not args.skip_rules:
            log.info("Running rules: %s%s",
                     ", ".join(rule_names or RULES),
                     " (dry run)" if args.dry_run else "")
            rules = run_rules(conn, camera_id=camera_id, since=since,
                              session_key=args.session_key, enabled=rule_names,
                              params=params, dry_run=args.dry_run)
            report["events"] = rules
            for name, r in rules["rules"].items():
                if r.get("error"):
                    log.warning("  %-20s FAILED (see traceback above)", name)
                elif args.dry_run:
                    log.info("  %-20s would emit %d", name, r["proposed"])
                else:
                    log.info("  %-20s %d new, %d already known",
                             name, r["new"], r["duplicate"])
            log.info("Events: %d new, %d suppressed as duplicates over %d tracks",
                     rules["inserted"], rules["duplicate"], rules["tracks"])
        else:
            log.info("--skip-rules: tracks only")
    finally:
        conn.close()

    report["duration_s"] = round(time.monotonic() - t0, 2)
    if overrides:
        report["param_overrides"] = overrides
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    log.info("Done in %.1fs", report["duration_s"])


if __name__ == "__main__":
    main()
