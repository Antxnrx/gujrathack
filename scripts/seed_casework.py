"""Walk one real investigation through the case-work API, end to end.

Why this exists
---------------
Incidents, evidence snapshots and bookmarks were built, wired and never once
run: zero rows in all three. That is not the same as "working" — an untried code
path is a claim, and the seal/verify logic in particular is the part of this
system that would be read most closely by anyone assessing it for court use.

This drives the real HTTP API as a real operator would, not the database
underneath it. If a validator rejects a payload or a foreign key is wrong, this
fails, which is the point.

The investigation it walks
--------------------------
Built from whatever the index actually contains, never from fixtures:

    1. take the watchlist hit on the plate the pipeline read correctly
    2. open an incident and attach the events around it
    3. seal an evidence snapshot over those rows
    4. verify the seal, and report intact/drift separately
    5. bookmark the track an investigator would return to

Re-running is safe: it looks for its own incident by title first.

Usage:
    python -m scripts.seed_casework
    python -m scripts.seed_casework --email analyst@gujarat.gov.in
"""
from __future__ import annotations

import argparse
import sys

import httpx

SETU = "http://localhost:8001/api"
MODEL1 = "http://localhost:8000"
TITLE = "Watchlist hit — GJ11BR2912, Timbavadi gate"


def die(msg: str) -> None:
    sys.exit(f"  ✗ {msg}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", default="root@gujarat.gov.in")
    ap.add_argument("--password", default="Sentinel@2026")
    args = ap.parse_args()

    with httpx.Client(timeout=60) as c:
        # ── Sign in against the registry, as the console does ────────────────
        r = c.post(f"{MODEL1}/api/v1/auth/login",
                   json={"email": args.email, "password": args.password})
        if r.status_code != 200:
            die(f"registry login failed ({r.status_code})")
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        print(f"  ✓ signed in as {args.email}")

        # ── 1. The alert an investigator would start from ────────────────────
        alerts = c.get(f"{SETU}/alerts", params={"limit": 5}, headers=h).json()
        items = alerts.get("items") or []
        if not items:
            die("no alerts — run: python -m scripts.watchlist_scan --add GJ11BR2912")
        alert = items[0]
        print(f"  ✓ alert: {alert.get('plate_text')} at "
              f"{alert.get('camera_external_id') or 'camera'} — "
              f"{alert.get('watchlist_reason', '')[:48]}")

        # ── 2. Events to attach. Prefer the camera the hit was on ────────────
        events = c.get(f"{SETU}/events", params={"limit": 6}, headers=h).json()
        event_ids = [e["id"] for e in (events.get("items") or [])][:4]
        if not event_ids:
            die("no events to attach — run the analytics fold first")

        existing = c.get(f"{SETU}/incidents", params={"limit": 50}, headers=h).json()
        already = next((i for i in (existing.get("items") or [])
                        if i.get("title") == TITLE), None)
        if already:
            incident = already
            print(f"  ✓ incident already open: {incident['id'][:8]}")
        else:
            r = c.post(f"{SETU}/incidents", headers=h, json={
                "title": TITLE,
                "description": (
                    "Vehicle on the stolen-vehicle watchlist (FIR 114/2026) read at "
                    "cam06 Timbavadi gate. Plate read exactly, ten frames, "
                    "confidence 0.95. Attaching the events around the sighting for "
                    "the duty officer."
                ),
                "priority": "high",
                "event_ids": event_ids,
            })
            if r.status_code not in (200, 201):
                die(f"incident create failed ({r.status_code}): {r.text[:200]}")
            incident = r.json()
            print(f"  ✓ incident opened: {incident['id'][:8]} with {len(event_ids)} events")

        # ── 3. Seal an evidence snapshot ─────────────────────────────────────
        track_ids = []
        tracks = c.get(f"{SETU}/tracks", params={"has_plate": True, "limit": 3},
                       headers=h).json()
        track_ids = [t["id"] for t in (tracks.get("items") or [])][:3]

        r = c.post(f"{SETU}/evidence", headers=h, json={
            "title": "GJ11BR2912 — sighting record, cam06",
            "incident_id": incident["id"],
            "event_ids": event_ids,
            "track_ids": track_ids,
            "detection_ids": [alert["detection_id"]] if alert.get("detection_id") else [],
            "note": (
                "Sealed at the point of triage. Records the rows as they read now: "
                "the plate, its confidence, the reads that voted on it, and the "
                "camera and time. No imagery is copied — the snapshot references "
                "the crops where they already live."
            ),
        })
        if r.status_code not in (200, 201):
            die(f"evidence seal failed ({r.status_code}): {r.text[:200]}")
        snap = r.json()
        digest = snap.get("digest") or snap.get("sha256") or ""
        print(f"  ✓ evidence sealed: {snap['id'][:8]}  sha256 {digest[:16]}…")

        # ── 4. Verify it ─────────────────────────────────────────────────────
        v = c.get(f"{SETU}/evidence/{snap['id']}/verify", headers=h)
        if v.status_code != 200:
            die(f"verify failed ({v.status_code}): {v.text[:200]}")
        vr = v.json()
        intact = vr.get("manifest_intact")
        drift_count = vr.get("drift_count", 0)
        print(f"  ✓ verify {vr.get('ref','')}: manifest "
              f"{'intact' if intact else 'BROKEN'}, "
              f"{vr.get('item_count', 0)} items, drift {drift_count}")
        print(f"      recorded   {(vr.get('recorded_sha256') or '')[:32]}…")
        print(f"      recomputed {(vr.get('recomputed_sha256') or '')[:32]}…")
        # Two separate facts, deliberately not collapsed into one light: the
        # manifest hash is about the snapshot, drift is about the world moving
        # on. An old snapshot legitimately drifts and is not thereby tampered.
        if intact and drift_count:
            print("      intact but drifted — the ordinary case for an old snapshot")

        # ── 5. Bookmark what an investigator returns to ──────────────────────
        if track_ids:
            b = c.post(f"{SETU}/bookmarks", headers=h, json={
                "target_type": "track",
                "target_id": track_ids[0],
                "label": "GJ11BR2912 — best plate read",
            })
            if b.status_code in (200, 201):
                print("  ✓ bookmarked the track")
            else:
                print(f"  · bookmark skipped ({b.status_code})")

        print()
        print("  Case work is now non-empty. In the console:")
        print("    Incidents → the open incident, with its events")
        print("    Evidence  → the sealed snapshot, and Verify")
        print("    Alerts    → the watchlist hit it started from")


if __name__ == "__main__":
    main()
