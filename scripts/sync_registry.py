"""Pull the camera estate from Model 1, the Sentinel CCTV Registry.

Why this exists
---------------
Model 2 has been keeping its own camera list, seeded from a hardcoded gateway.
Model 1's handover is explicit about that being the wrong shape:

    "Pick the endpoint whose reachability matches your network.
     Do not keep your own camera list; ask the registry."

Two things go wrong when the analytics layer owns its own estate. A camera
onboarded in the registry is invisible here until someone edits a seed script,
so the two systems disagree about what exists. And the stream URL is baked into
Model 2's configuration, which means a department that moves a camera behind a
different gateway breaks inference with no error anyone can act on.

After this runs, the registry is the single source of truth for *which* cameras
exist and *how to reach them*, and Model 2 owns only what it observes through
them. That is the whole integration, and it is deliberately one-directional:
nothing here writes back to Model 1.

What it fills in
----------------
``migrations/002_backend.sql`` already added the columns for this and left them
empty — ``camera_uid`` even carries the comment "Model 1 join key". They are the
reason ``/api/cameras/systems`` can group an estate by owning department without
Model 2 modelling departments at all:

    camera_uid                       GJ-SEN-000001, the registry's own identifier
    district, area                   from the registry's geocoding
    department                       resolved from the registry's department list
    lat, lng                         surveyed or district-level, per the registry
    ingest_url                       chosen from /streams by reachability
    metadata.source_system           which VMS owns the camera
    metadata.stream_protocol         hls / rtsp / whep
    metadata.geocode_precision       'surveyed' vs 'district' — carried, not hidden
    metadata.model1_camera_id        the registry's UUID, for later lookups

Matching is on ``external_id`` (cam01…cam30), which both sides already use as
the vendor's own identifier.

Usage
-----
    python -m scripts.sync_registry                 # sync, report what changed
    python -m scripts.sync_registry --dry-run       # show the diff, write nothing
    python -m scripts.sync_registry --prune         # also deactivate cameras the
                                                    # registry no longer lists
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import httpx
import psycopg2
import psycopg2.extras

log = logging.getLogger("setu.sync")

# Which stream to record as the one the worker should pull. Ordered by how
# widely reachable each is: a public CDN works from anywhere, a direct IP needs
# gateway ports open, and lan_only does not leave the site. The worker may still
# be on the wrong side of that boundary, which is why the whole list is kept in
# metadata rather than only the winner.
REACHABILITY_RANK = {"public_cdn": 0, "direct_ip": 1, "lan_only": 2}
PROTOCOL_RANK = {"hls": 0, "rtsp": 1, "whep": 2}


def _settings() -> dict:
    """Model 1 connection details, from the same .env that drives everything else."""
    base = os.getenv("MODEL1_BASE_URL", "http://localhost:8000").rstrip("/")
    return {
        "base": base,
        "api_key": os.getenv("MODEL1_API_KEY", ""),
        "email": os.getenv("MODEL1_EMAIL", ""),
        "password": os.getenv("MODEL1_PASSWORD", ""),
        "db": os.getenv("DATABASE_URL", "postgresql://setu:setu_dev_only@localhost:5434/setu"),
    }


def _auth_headers(cfg: dict, client: httpx.Client) -> dict:
    """Prefer the service API key; fall back to an operator login.

    The API key is the right credential for a background job — it is not tied to
    a person and does not expire mid-run. The login path exists because a local
    developer has demo accounts and may not have minted a key yet.
    """
    if cfg["api_key"]:
        log.info("Authenticating to Model 1 with service API key")
        return {"X-API-Key": cfg["api_key"]}

    if not (cfg["email"] and cfg["password"]):
        sys.exit(
            "No Model 1 credentials. Set MODEL1_API_KEY, or MODEL1_EMAIL and "
            "MODEL1_PASSWORD, in .env"
        )

    log.info("Authenticating to Model 1 as %s", cfg["email"])
    r = client.post(
        f"{cfg['base']}/api/v1/auth/login",
        json={"email": cfg["email"], "password": cfg["password"]},
        timeout=20,
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def fetch_departments(cfg: dict, client: httpx.Client, headers: dict) -> dict[str, str]:
    r = client.get(f"{cfg['base']}/api/v1/departments", headers=headers, timeout=30)
    r.raise_for_status()
    return {d["id"]: d["code"] for d in r.json()}


def fetch_cameras(cfg: dict, client: httpx.Client, headers: dict) -> list[dict]:
    """Every camera the registry lists, paged."""
    out: list[dict] = []
    offset, limit = 0, 100
    while True:
        r = client.get(
            f"{cfg['base']}/api/v1/cameras",
            params={"limit": limit, "offset": offset},
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        out.extend(page["items"])
        offset += limit
        if offset >= page.get("total", 0) or not page["items"]:
            break
    return out


def fetch_streams(cfg: dict, client: httpx.Client, headers: dict, camera_id: str) -> list[dict]:
    r = client.get(
        f"{cfg['base']}/api/v1/cameras/{camera_id}/streams", headers=headers, timeout=30
    )
    if r.status_code != 200:
        return []
    return r.json()


def pick_stream(streams: list[dict]) -> dict | None:
    """The endpoint the worker should pull from.

    Sorted by reachability first and protocol second, with the registry's own
    ``is_primary`` flag breaking ties — it reflects what the source said about
    itself, which outranks our preference between two equally reachable options.
    """
    if not streams:
        return None
    return sorted(
        streams,
        key=lambda s: (
            REACHABILITY_RANK.get(s.get("reachability") or "", 9),
            0 if s.get("is_primary") else 1,
            PROTOCOL_RANK.get(s.get("protocol") or "", 9),
        ),
    )[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    ap.add_argument("--prune", action="store_true",
                    help="mark cameras absent from the registry as inactive")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    cfg = _settings()
    with httpx.Client(follow_redirects=False) as client:
        headers = _auth_headers(cfg, client)
        departments = fetch_departments(cfg, client, headers)
        cameras = fetch_cameras(cfg, client, headers)
        log.info("Registry lists %d cameras across %d departments",
                 len(cameras), len(departments))

        conn = psycopg2.connect(cfg["db"])
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        inserted = updated = unchanged = no_stream = 0
        seen: list[str] = []

        for cam in cameras:
            ext = cam.get("external_camera_id")
            if not ext:
                continue
            seen.append(ext)

            streams = fetch_streams(cfg, client, headers, cam["id"])
            stream = pick_stream(streams)
            if stream is None:
                no_stream += 1

            meta = cam.get("metadata") or {}
            dept_code = departments.get(cam.get("department_id") or "", None)

            # source_system names the VMS that owns the camera. The registry
            # models that as the connector the camera was onboarded through; the
            # UID prefix is the visible form of it and is what an operator will
            # recognise on screen.
            uid = cam.get("camera_uid") or ""
            source_system = (
                uid.rsplit("-", 1)[0] if uid.count("-") >= 2
                else (dept_code or "unknown")
            )

            new_meta = {
                "source_system": source_system,
                "stream_protocol": (stream or {}).get("protocol"),
                "reachability": (stream or {}).get("reachability"),
                "requires_auth": (stream or {}).get("requires_auth"),
                "geocode_precision": meta.get("geocode_precision"),
                "model1_camera_id": cam["id"],
                "camera_type": cam.get("camera_type"),
                "connectivity": cam.get("connectivity"),
                "ownership_class": cam.get("ownership_class"),
                "site_type": cam.get("site_type"),
                # Every endpoint, not just the chosen one: a worker on a
                # different network segment needs the alternatives, and losing
                # them here would mean going back to the registry per frame.
                "streams": streams,
            }

            row = {
                "external_id": ext,
                "name": cam.get("name"),
                "lat": cam.get("latitude"),
                "lng": cam.get("longitude"),
                "ingest_url": (stream or {}).get("url"),
                "camera_uid": uid or None,
                "district": meta.get("geocode_district"),
                "area": cam.get("address"),
                "department": dept_code,
                "geocode_precision": meta.get("geocode_precision") or "district",
                "metadata": json.dumps(new_meta),
            }

            cur.execute("SELECT camera_uid, ingest_url, district FROM cameras WHERE external_id=%s",
                        (ext,))
            existing = cur.fetchone()

            if args.dry_run:
                if existing is None:
                    inserted += 1
                elif (existing["camera_uid"] != row["camera_uid"]
                      or existing["ingest_url"] != row["ingest_url"]
                      or existing["district"] != row["district"]):
                    updated += 1
                else:
                    unchanged += 1
                continue

            # The registry owns identity and reachability; Model 2 owns what it
            # observed. So this updates the registry-derived columns and never
            # touches status, which the health probe maintains from its own
            # evidence.
            cur.execute(
                """
                INSERT INTO cameras (external_id, name, lat, lng, ingest_url,
                                     camera_uid, district, area, department,
                                     geocode_precision, metadata)
                VALUES (%(external_id)s, %(name)s, %(lat)s, %(lng)s, %(ingest_url)s,
                        %(camera_uid)s, %(district)s, %(area)s, %(department)s,
                        %(geocode_precision)s, %(metadata)s::jsonb)
                ON CONFLICT (external_id) DO UPDATE SET
                    name              = EXCLUDED.name,
                    lat               = COALESCE(EXCLUDED.lat, cameras.lat),
                    lng               = COALESCE(EXCLUDED.lng, cameras.lng),
                    ingest_url        = COALESCE(EXCLUDED.ingest_url, cameras.ingest_url),
                    camera_uid        = EXCLUDED.camera_uid,
                    district          = EXCLUDED.district,
                    area              = EXCLUDED.area,
                    department        = EXCLUDED.department,
                    geocode_precision = EXCLUDED.geocode_precision,
                    metadata          = cameras.metadata || EXCLUDED.metadata
                RETURNING (xmax = 0) AS was_insert
                """,
                row,
            )
            if cur.fetchone()["was_insert"]:
                inserted += 1
            else:
                updated += 1

        pruned = 0
        if args.prune and not args.dry_run and seen:
            cur.execute(
                "UPDATE cameras SET status='retired' "
                "WHERE external_id <> ALL(%s) AND status <> 'retired'",
                (seen,),
            )
            pruned = cur.rowcount

        if not args.dry_run:
            conn.commit()

        verb = "would be" if args.dry_run else ""
        log.info("%s%d inserted, %d updated, %d unchanged, %d retired",
                 f"{verb} " if verb else "", inserted, updated, unchanged, pruned)
        if no_stream:
            log.warning("%d cameras have no stream endpoint in the registry", no_stream)

        if not args.dry_run:
            cur.execute("""
                SELECT COALESCE(metadata->>'source_system','unknown') AS system,
                       department, COUNT(*) AS cameras,
                       COUNT(DISTINCT district) AS districts,
                       COUNT(ingest_url) AS with_stream
                FROM cameras GROUP BY 1,2 ORDER BY cameras DESC
            """)
            print()
            print(f"{'source system':<18}{'dept':<8}{'cameras':>8}{'districts':>10}{'streams':>9}")
            print("-" * 53)
            for r in cur.fetchall():
                print(f"{r['system']:<18}{(r['department'] or '—'):<8}"
                      f"{r['cameras']:>8}{r['districts']:>10}{r['with_stream']:>9}")


if __name__ == "__main__":
    main()
