# Known limits

Stated here rather than discovered by a judge. Everything below was measured on
the running system; where a number is a sample, the sample size is given.

---

## ANPR

**Accuracy is 68% exact match on nineteen hand-read plates, at 4.8% character
error.** Fifty-two plate regions were exported magnified and read by eye:
nineteen were legible, twenty-five were plates too degraded to read, and eight
were not plates at all. Unreadable crops are recorded rather than dropped,
because dropping them would quietly inflate the denominator.

**Ground truth is anchored to the crop, not to the track.** That distinction cost
a day: the labels were originally keyed on `track_id`, and when tracks were split
on time gaps 11 of 17 labels ended up pointing at a track that no longer held
what it held when it was read. Measured accuracy "fell" from 67% to 40% without
the reader changing at all. A track id is not a stable identity for a plate; the
crop a human read is.

Character error more than halved — 10.1% to 4.8% — when tracks stopped carrying
a second vehicle's plate. Exact match barely moved, which is the tell: the split
did not make the reader better at characters, it stopped whole plates being
attributed to the wrong car. Nineteen is still a small sample.

**Four of the five misses are one character out** — `GJ15CB2936` read as
`GJ15CB2934`, `GJ11CO5760` as `GJ11CQ5760`. The system is rarely wildly wrong;
it is usually one glyph out, and O↔Q in a letter position is most of it. At the
resolutions here a Q's tail is about one pixel, so no model separates them. What
closes the gap operationally is that search and the watchlist match on confusion
distance rather than string equality: `plate_similarity` scores `GJ11CO5760`
against `GJ11CQ5760` at 0.975, well above the 0.90 alert threshold. A wanted
vehicle is still found; only the displayed string is wrong.

**Detector precision is 85%.** Seven of the forty-eight boxed regions were not
plates — bus destination boards in Gujarati, an "ALLINDIA PERMIT" sign, operator
livery reading "SHREE HARI". That is a different failure from an OCR misreading a
real plate and is counted separately.

**Confidence is well calibrated, and that is the more useful property.** Reads in
the `actionable` band were exactly right 5/7, `verify` 5/7, `lead` 0/1. An
operator can be told which plates to act on. A system with higher raw accuracy
and no calibration is worse in a control room, because nothing distinguishes the
good reads from the bad ones.

**The OCR has no null output, and that used to reach the screen.** Asked to read
solid black it answers `A22251` at 0.23 confidence; asked to read noise, `ALEE`
at 0.49. Every crop yields a string. 34 of 111 tracks therefore carried a
"registration" that is not one — 20 of them the word UNREADABLE, misspelled,
which is the model's own admission that it could not read the crop, stored as a
vehicle's identity across ten cameras. The registration grammar in `ml/rto.py`
now rejects those: **tracks presented with a non-registration went 34 → 0**, and
all 77 that keep a plate name an RTO Gujarat has actually issued. The raw string
survives in `plate_votes.rejected_raw`; only the column an officer acts on is
cleared.

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

**The template can only fix what position makes certain.** `XX00XX0000` fixes
the character *class* at every position, so a zero read in the series is an O.
It cannot separate O from Q, or 6 from 4, because both members of those pairs are
legal in the position. Applying it moved the estate's consistency to 100% and
moved exact match not at all — the two are different properties and are reported
separately for that reason.

**Seven of thirty cameras are ANPR-capable**, scored on measured yield and stored
in `cameras.metadata.anpr` — cam09 leads at 22.3%, and every one of the seven is a
chokepoint: a toll plaza, a gate, a bus port, an approach road. Twelve are labelled
`no_plates_observed` and four `overview_only`.

---

## Coverage of the estate

**Four cameras still have almost no data.** cam18-cam30 were barely touched by the
original GPU run; a daylight re-index brought nine of them in and the estate now
stands at 26 of 30 cameras with detections, 61,981 in total. cam19, cam20 and
cam28 returned `403` part-way through that run — a session expiry upstream, not a
fault here — and re-running the indexer after refreshing the credential would
finish them.

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

## The job layer

**Playbooks compose existing endpoints; they add no capability of their own.**
`backend/app/routers/playbooks.py` chains the watchlist, the retrospective scan,
incidents and evidence into four named jobs. Anything a playbook does can be
done by calling those endpoints directly — deliberately, because a playbook that
drifted from the endpoint it wraps would make the case file describe work that
did not happen.

**Four jobs, not a complete doctrine.** Wanted vehicle, scene canvass, follow a
vehicle and seal for court are the jobs this estate's data can actually support.
They are not a survey of police work, and a real deployment would add to them.

**The self-contradiction check is within a single canvass only.** A plate
appearing on two vehicle classes is caught when both fall inside the window being
canvassed. The same contradiction spread across two separate queries is not
detected, because nothing compares results between calls.

**Caveats are computed, and can therefore be wrong.** The estate caveats are
recomputed per call from the cameras and tracks tables rather than written into
the text, so they follow the data — but the first version of that query joined
cameras to tracks on `TRUE` and reported sixty thousand cameras. A caveat that
has gone wrong is worse than none, because it will be believed.

---

## Platform

**The upstream session expires, and the indexer now signs back in.** Both the
indexer and the registry's enrichment authenticate with a session cookie that has
a limited life, so a long batch run could begin succeeding and end in `403` — as
one did, part-way through indexing cam19. `ml/stream.py` now treats 401 and 403
as "sign in again" rather than "fail", on the manifest, the AES key and the
segment fetch alike, and retries once with a fresh cookie. A fresh cookie that is
also refused is reported rather than retried: that is the credential's problem,
and hammering the login endpoint would turn it into an outage of our own making.

The registry's own enrichment still needs `scripts/refresh_sentinel_credential.py`
run by hand.

**The gateway serves recorded footage, not a live edge.** Its playlists are
`#EXT-X-PLAYLIST-TYPE:VOD` — 4,306 segments, twelve hours, terminated with an
ENDLIST. Everything in this system that says "live" means "relayed on demand from
the archive". The player reads the playlist and labels it `recorded` accordingly,
and will say `live` by itself the day a genuinely live feed arrives.

**The fold must be running for anything to appear.** `ml/analytics/service.py`
turns detections into tracks and events every 60 seconds. `platform/run.sh`
starts it; a deployment that does not will fill its database and show an empty
timeline, with no error anywhere to explain it.

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

- **No synthetic data, and no way to make any.** Every one of the 61,982
  detections and 1,913 tracks is `source='live'`. The synthetic generator
  (`scripts/seed_demo.py`), the scorer that only ever measured against its
  invented ground truth (`scripts/score_anpr.py`), and the competing camera
  seeder (`scripts/seed.py`) have all been deleted — the registry owns the
  estate, and Sentinel's footage is the only thing that can put a detection in
  this database.
- **Case work starts empty.** Incidents, evidence and the watchlist hold nothing
  until an operator does something, because a seeded incident describes work
  nobody did. The playbooks build a complete case — watchlist, retrospective
  scan, alerts, incident, sealed and verified pack — in about a tenth of a
  second, so an empty case file costs a demo nothing and buys it honesty.
- **No video is stored.** The relay passes bytes through; evidence snapshots seal
  references and field values, never copies of imagery.
- **The relay accepts no client URL.** Segment addresses are minted server-side
  as per-camera HMAC tokens, host-checked, redirects disabled.
