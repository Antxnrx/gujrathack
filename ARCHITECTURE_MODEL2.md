# Model 2 — architecture note

**How departmental systems stay unaffected, and where the second VMS attaches.**

Written against the running deployment on 2026-09-06. Every figure below came from the
live API, not from a design document; the endpoint that produced each one is named so it
can be re-checked.

---

## 1. The requirement, and the shape of the answer

The problem statement asks for a unified viewing platform that connects **directly** to
departmental CCTV/VMS — no intermediate middleware, no federation layer — while the
existing departmental VMS and storage keep operating unaffected, and without
centralising the video itself.

Those two constraints pull against each other, and the resolution is the whole design:

> **Video is borrowed. Metadata is kept.**

Frames are read, analysed, and dropped. What persists is the *description* of what was in
them — a row saying a black auto-rickshaw carrying plate GJ21VQ7995 passed camera cam04
at 15:15:39. That row is a few hundred bytes. The footage it describes stays where it has
always been, on the department's own recorder, under the department's own retention
policy.

## 2. What is actually connected today

`GET /api/cameras/systems`:

| Source system | Department | Cameras | Districts | Protocol | UID prefix |
|---|---|---:|---:|---|---|
| Gujarat Police — Sentinel VMS | police | 16 | 7 | HLS | `GJ-POL-…` |
| AMC City Surveillance | municipal | 7 | 5 | HLS | `GJ-MUN-…` |
| SHA Highway Cameras | highways | 7 | 5 | HLS | `GJ-HWY-…` |

**30 cameras across three source systems and three departments**, which satisfies the
"at least two different systems" deliverable with one to spare. The console renders this
table from that endpoint rather than asserting it — *Dashboard → Connected systems*.

**Be precise about what this demonstrates.** In this sandbox all three systems are served
by one HLS gateway standing in for three. What is genuinely built is the part that
matters for the claim: the estate is *modelled* as N independent systems, every camera
carries its own `source_system`, `department`, `camera_uid` and `stream_protocol`, and
every query path — search, the transition graph, the reconstruction — already crosses
those boundaries without knowing or caring that they exist. Pointing the second system at
a real ONVIF recorder is a per-camera `ingest_url` and an adapter; it is not a redesign.

### Where a new system attaches

One place: `cameras.metadata`.

```
cameras.metadata->>'source_system'     which VMS owns this camera
cameras.metadata->>'stream_protocol'   how to reach it (hls today; rtsp/onvif next)
cameras.camera_uid                     the department's own identifier, preserved
cameras.ingest_url                     the address, resolved server-side only
```

Nothing downstream branches on source system. `/api/cameras/systems` is a `GROUP BY` over
that column — a fourth department appears in the UI by inserting camera rows, with no code
change.

## 3. Why there is no middleware, and what the relay is instead

The console talks to the departmental gateway directly. There is no message broker, no
federation service, no shared database between departments.

There *is* a thin HLS relay (`backend/app/routers/stream.py`), and it is worth being exact
about why it exists, because "relay" and "middleware" are easy to confuse:

1. **Auth** — the gateway's HLS endpoints sit behind a session cookie from a form login.
   A browser cannot hold it, and the shared credential must not be shipped to front-end
   JavaScript.
2. **CORS** — the gateway sends no `Access-Control-Allow-Origin`, so `fetch` of the
   playlist from the console's origin fails before it starts, and hls.js needs `fetch`.
3. **User-Agent** — the gateway 403s any request without a browser-shaped UA.

Three protocol problems, none of them a reason to hold video. **Bytes pass through and are
never written to disk or to object storage.** The relay is a permission-shaped adapter,
not a tier.

### The relay accepts no URLs

A URL-taking proxy inside a police network is an SSRF gateway to every internal service.
This one takes none. The playlist address is built server-side from configuration plus the
camera's `external_id`; while rewriting the playlist, each segment and key URI is resolved,
checked to be on the gateway's own scheme+host+port, and replaced with an **opaque
per-camera HMAC token**. The token→URL map is the only way a URL can be reached, tokens are
scoped to the camera they were minted for, and redirects are not followed. There is no
client-supplied string anywhere on the path to the HTTP client — not a host, not a path,
not a query.

## 4. What the departmental system experiences

| Concern | Effect |
|---|---|
| Storage | **None.** No video is written by this platform. |
| Retention | **Unchanged.** The department's policy is the only policy. |
| Write access | **None.** Every call is a read: `GET` the playlist, `GET` the segments. |
| Load | One viewer's worth per operator watching, plus batch reads during indexing. |
| Configuration | One read-only service account. No agent installed, no VMS reconfiguration. |
| Failure | If the gateway goes down, its cameras stop playing here. Nothing else is affected. |

The last row is not hypothetical, and it is the honest state of this demo: the sandbox
gateway is **currently refusing this service's login with a 403**. The console reports it
as `gateway_auth` with the sentence *"The departmental gateway refused this service's
login. Credentials or IP allow-listing, upstream — not the console"* — and every other
feature keeps working, because the metadata was already indexed. That is the isolation
property demonstrated by accident, which is the best way to demonstrate it.

## 5. What is centralised, and what that buys

52,589 detections and 3,553 tracks are indexed from footage that this platform does not
hold a frame of (`GET /api/health`). Because that index is central while the video is not,
questions can be asked across departments that no single department can answer alone:

- **Search** — one endpoint over time, place, camera, plate, colour, class and free text.
- **Camera transition graph** — 67 edges over 30 cameras: 13 learned from observed plate
  transitions, 52 geographic, 2 operator-confirmed (`GET /api/investigate/graph`).
- **Cross-camera reconstruction** — a route assembled from cameras owned by *different
  departments*, which the reconstruction view counts and names explicitly.

A police camera and a municipal camera contribute to the same route. Neither department
gave up its footage to make that possible.

## 6. Evidence without centralised video

An evidence snapshot (`POST /api/evidence`) records the rows as they read at sealing time
plus a SHA-256 over a canonical serialisation of them. It stores **references and field
values, never copies of imagery** — which is precisely what lets the no-central-storage
promise and a usable case file coexist.

`GET /api/evidence/{id}/verify` re-computes the digest and separately diffs the sealed
values against the live ones, reporting two independent facts:

- **Seal intact** — the manifest still hashes to the recorded digest. *About the snapshot.*
- **Drift** — live rows now differ from what was sealed. *About the world, and not tampering.*

An intact seal with drift is the ordinary case for any snapshot older than a shift. The
console renders them as separate badges for that reason; collapsing them into one
"valid/invalid" light would train operators to ignore the signal that matters.

## 7. Deployment shape

```
Departmental systems (unchanged, authoritative for their own video)
  ├─ Gujarat Police — Sentinel VMS      16 cameras
  ├─ AMC City Surveillance               7 cameras
  └─ SHA Highway Cameras                 7 cameras
        │  read-only: HLS playlist + segments, one service account each
        ▼
  Setu API  (FastAPI)
    · stream relay      — pass-through, host-locked, HMAC tokens, stores nothing
    · metadata index    — Postgres: detections, tracks, events, zones, alerts
    · analytics         — 7 detector rules; multi-frame ANPR consensus
    · reconstruction    — likelihood-ratio scoring across the camera graph
        │  /api on the console's own origin
        ▼
  Console (React + Vite)  — 19 kB gzipped initial payload; hls.js and Leaflet
                            load only on the two routes that need them
```

Model 1 (infrastructure: RS256 JWTs via JWKS, plus a service API key) plugs in through
`model1_base_url` / `model1_jwks_url` / `model1_api_key`. With none set the API runs in
`auth_optional` mode, which is what makes the offline demo possible — and that mode is
**refused** when `require_auth` is on, so a production deployment cannot fall into it by
forgetting a variable.

## 8. Known gaps, stated plainly

1. **The Sentinel gateway is refusing login (403).** External; no camera is playable right
   now. Every metadata-driven feature is unaffected.
2. **No imagery.** The MinIO bucket `setu-clips` does not exist and no row carries a
   `thumbnail_key`, so every `thumbnail_url` is null. The console shows labelled
   placeholders and a `no imagery` badge in the topbar rather than broken images.
3. **One gateway stands in for three systems.** See §2 — the modelling is real, the
   physical separation is not.
4. **`camera_links` needs `POST /api/investigate/graph/rebuild`** after any batch index,
   or the graph reflects the previous run.
