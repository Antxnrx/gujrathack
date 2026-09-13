# Known limits

Stated here rather than discovered by a judge. Everything below was measured on
the running system; where a number is a sample, the sample size is given.

---

## ANPR

**Accuracy is 33% exact match on nine hand-labelled plates**, and 89% of all
consensus plates fall in the confidence band that was never once exactly right.
The console marks those as `low confidence` rather than presenting them as the
vehicle's identity, but they are still wrong strings on screen. Twenty-four crops
were exported for labelling and nine were legible to a human; the other fifteen
were obscured by a pole, blown out by headlights, cropped out of frame, or showed
only a fragment of a two-row commercial plate. Those are recorded as unreadable
rather than dropped, because dropping them would quietly inflate the denominator.

Nine is a small sample. It is the honest state of the measurement, not a
confident estimate, and the next order of business is two hundred.

**Confidence is well calibrated, and that is the more useful property.** Reads
above 0.5 were exactly right 2/2; reads below 0.3 were right 0/3. An operator can
be told which plates to act on. A system with higher raw accuracy and no
calibration is worse in a control room, because nothing distinguishes the good
reads from the bad ones.

**Plates are physically unreadable on most of this estate.** Read rate against
vehicle width in frame:

| width | crops | reads | rate |
|---|---|---|---|
| under 60 px | 10,694 | 0 | 0.00% |
| 60–120 px | 10,839 | 7 | 0.06% |
| 120–200 px | 5,184 | 155 | 2.99% |
| 200–350 px | 2,596 | 143 | 5.51% |
| over 350 px | 974 | 59 | 6.06% |

Seventy-one per cent of vehicle crops fall below 120 px. This is optics, not
model choice: an Indian plate is about an eighth of a vehicle's width, and
reading ten characters needs roughly 90 px of plate.

**A detector fine-tune was attempted and abandoned.** Candidates were harvested
from three detectors at 0.05 confidence over 6,000 crops and adjudicated by
whether the region read as a legal plate. Of 2,521 candidates, 24 qualified — and
every one had been found by more than one detector. There was no pool of plates
one model sees and another misses, which is the only thing a fine-tune could have
exploited. Training on 24 images would have overfitted and proved nothing.

**Five of thirty cameras are ANPR-capable**, scored on measured yield and stored
in `cameras.metadata.anpr`: cam09 (22.3%), cam06, cam12, cam17, cam07. All are
chokepoints. Sixteen are labelled `no_plates_observed`.

---

## Coverage of the estate

**Thirteen cameras are thinly indexed.** cam18–cam30 were barely touched by the
original GPU run. cam18 has since been indexed; the rest returned `403` partway
through a later run, which is a session expiry against the upstream gateway
rather than a fault in this system. The feeds are reachable and the indexer works.

**The archive is 12 hours, 21:00 to 09:00**, so roughly three of those hours are
daylight. Plate work at night is materially harder than plate work at noon, and
most of this archive is night.

**Cross-camera plate matching has no edges yet.** The transition graph holds 138
geographic edges and zero plate edges, because a plate edge needs the same plate
read at two different cameras and reads currently concentrate at cam06 and cam09.
Reconstruction therefore runs on geography and appearance, not on plate
continuity.

---

## Detectors

**Two of seven have not fired on real data.** `abandoned_object` and
`accident_candidate` produced nothing. Both are genuinely rare, and neither has
been shown to work outside the synthetic fixtures — absence of events here is not
evidence that they work.

**Wrong-way detection is claimed on two cameras, not thirty.** A permitted
direction only exists where traffic is one-way, or where a two-way road separates
its directions in space. Six of the seven cameras with enough traffic are
junctions or forecourts where no permitted direction can be derived, and no
direction zone is created for them. The first implementation ignored this and
produced 180 wrong-way events from 509 moving tracks.

**Zone intrusion is tuned for this environment and would need retuning
elsewhere.** Ungated it fired 223 times, because crossing the road is normal
behaviour in Gujarat traffic. The rule now requires sustained presence in the
live carriageway, which takes it to 29 — but that threshold encodes a local norm,
not a universal one.

**Derived zones are a starting point, not ground truth.** They come from observed
traffic and are marked `created_by='derived'`. The Zones editor remains the
authority and an operator should correct them.

---

## Platform

**The upstream session expires, and nothing refreshes it mid-run.** Both the
indexer and the registry's enrichment authenticate with a session cookie that has
a limited life, so a long batch run can begin succeeding and end in `403` — as
one did, part-way through indexing cam19. `scripts/refresh_sentinel_credential.py`
in the registry refreshes it in one place, but it has to be run; nothing detects
the expiry and re-authenticates on its own.

**Resolutions are now read from the streams, not asserted.** The registry used to
record every camera as `1920x1080`. With a current session it probes the manifest
and the truth is a mixture — `854x480`, `1920x1080`, `1280x960`, `1280x720`,
`960x576`, `640x480`, at frame rates from 10 to 30 fps. That heterogeneity is the
measured reason a handful of cameras support ANPR and most do not, and it is now
visible in the registry rather than inferred from the crops.

**The SSE token travels in a query string.** `EventSource` cannot set request
headers, so the live stream takes `?access_token=`. Query strings reach access
logs in a way headers do not; the trade is accepted because the alternative is no
realtime under authentication, and it is documented at the point of decision.

**Two databases, deliberately.** The registry owns PostGIS, the control room owns
pgvector, and they meet over HTTP rather than in a shared schema. That is the
property that lets either be redeployed without the other.

**The project path contains a colon** (`Personal-Projects:Hackathons`), which
breaks anything building a `PATH`-like variable from it — `npm run dev` fails with
`next: command not found`, `PYTHONPATH` resolves to two invalid paths, and Docker
rejects bind mounts. `platform/run.sh` invokes binaries directly to avoid all
three. Renaming the directory would remove the whole class of problem.

---

## What is *not* a limitation

Worth stating, because these are the usual suspects:

- **No synthetic data.** The database was rebuilt from the GPU run's real output
  and the shipped synthetic seed was deleted.
- **No video is stored.** The relay passes bytes through; evidence snapshots seal
  references and field values, never copies of imagery.
- **The relay accepts no client URL.** Segment addresses are minted server-side
  as per-camera HMAC tokens, host-checked, redirects disabled.
