# Demo script

Twelve minutes, four acts. Every figure below is live from the running system —
if a number on screen disagrees with this page, the screen is right and this page
is stale.

```bash
./platform/run.sh          # everything, ~40 seconds
```

| | |
|---|---|
| Control room | http://localhost:5173 |
| Registry | http://localhost:3000 |
| Sign in | any registry account · password `Sentinel@2026` |

Use **`analyst@gujarat.gov.in`** for the demo. It can read and export but not
write, which is the right posture for someone driving a projector, and it proves
the role model is real rather than decorative.

---

## Act 1 — One account, two systems (90 seconds)

Open the control room cold. It asks for a sign-in.

> "This console has no accounts of its own. Identity belongs to the registry —
> Model 1 — which issues the token and publishes the keys. The analytics layer
> verifies the signature and never sees a password."

Sign in as the analyst. The shell loads.

**What to point at:** *Connected systems* on the dashboard reads
`GJ-SEN · SEN · 10 districts · GJ-SEN-000001`. That is a `GROUP BY` over camera
metadata, not a caption — the estate, its owning department and its UID scheme
all came from the registry over HTTP.

> "If a department onboards a camera in the registry tomorrow, it appears here
> without anyone editing this system."

Click **Registry ↗** in the sidebar to show the other half, then come back.

---

## Act 2 — What the cameras saw (3 minutes)

**Dashboard → the counters.** 30 cameras, 55,195 detections, 1,665 tracks.

> "Real footage. Every detection here came from a frame of Gujarat CCTV, and the
> crop behind each one is on disk — nothing on this screen was generated."

**Timeline.** Five detector types have fired: zone intrusion, loitering, crowd,
wrong-way, stopped vehicle.

Open a *zone intrusion*: **"Person in 11 dolatpara-junagadh — live carriageway"**.

> "Nobody drew that zone. The carriageway is the ground vehicles actually used,
> derived from the tracks themselves — and it is deliberately not the full area
> they covered, because that would include the footpath and flag every pedestrian
> near the road."

**The honest part, and say it out loud:** ungated, this rule fired 223 times.

> "Crossing the road is not an anomaly in Gujarat traffic, it is Tuesday. So the
> rule asks a different question: not *did someone cross*, but *did someone
> stay*. That took it from 223 to 29."

**Zones & rules** shows the derived polygons if anyone wants to see them.

---

## Act 3 — A plate, and why you should believe it (4 minutes)

This is the act that wins or loses the room. Do not rush it.

**Search → plate `GJ11BR`.** One track returns: a white truck at cam06
Timbavadi gate, `GJ11BR2912`, 66% confidence, 10 reads.

Open it and show the **per-character vote**.

> "Most systems show you a plate and a percentage. A percentage cannot tell you
> whether ten frames agreed on every character or two frames agreed on six — both
> round to the same number and they are not the same evidence.
>
> So we show the ballot. Each column is one character position, and the bars are
> what the frames voted. An amber column is where the runner-up was nearly as
> well supported — that is precisely where the plate is most likely wrong, and it
> is the column an operator should search both ways."

Then the measurement, unprompted:

> "We read 24 crops by eye to get ground truth. Nine were legible; the rest were
> behind a pole, blown out by headlights, or had the plate out of frame, and we
> recorded them as unreadable rather than quietly dropping them.
>
> Against those nine: **33% exact match**. That is not a good number and we are
> not going to dress it up.
>
> This is the number that matters." — show the calibration:

| confidence | n | exact |
|---|---|---|
| 0.0–0.3 | 3 | **0%** |
| 0.3–0.5 | 4 | 25% |
| 0.5–0.7 | 2 | **100%** |

> "Every read above 0.5 was exactly right. Every read below 0.3 was wrong. The
> system knows when to be believed, which is worth more to a control room than a
> higher headline number they cannot tell apart. An operator acts on the green
> ones and treats the rest as leads."

If asked *why only 33%* — that is Act 4, and it is a good question to be asked.

---

## Act 4 — Where it works, and where it cannot (3 minutes)

**Cameras → sort by ANPR capability.**

> "We measured every camera on what it actually delivered, and five of thirty can
> do ANPR: cam09 at 22%, then a gate, a toll plaza, a bus port and an approach
> road. Every one is a chokepoint — somewhere vehicles slow down and pass close
> to the lens."

Then the measurement that explains it:

| vehicle width in frame | crops | plate reads |
|---|---|---|
| under 60 px | 10,694 | **0** |
| 60–120 px | 10,839 | 7 |
| 120–200 px | 5,184 | 155 |
| over 350 px | 974 | 59 |

> "Zero reads from ten thousand crops under sixty pixels. Those plates are not
> being missed — they were never sampled. Seventy-one per cent of this estate
> sits below the threshold where a plate is physically resolvable.
>
> We tried to fix it with a better detector. We harvested candidates from three
> models at low confidence over six thousand crops; twenty-four read as legal
> plates, and every one had already been found by more than one model. There was
> no recall gap to train on. The constraint is optics, not the network.
>
> Which is also how ANPR is really deployed. Nobody runs it off a wide-angle
> junction camera. You site a dedicated camera low and zoomed at a chokepoint —
> and this estate already has five of them. So the system says which cameras can
> answer a plate question and which cannot, instead of silently returning
> nothing."

**Close on Evidence.** Seal a snapshot, then verify it.

> "Two separate facts: the seal is intact, and the live data has drifted. An old
> snapshot legitimately drifts. Collapsing those into one valid/invalid light is
> how you train an operator to ignore the one that matters."

---

## Questions you should want

**"Is any of this synthetic?"**
No. The database was rebuilt from the GPU run's own output; the synthetic seed
the project shipped with was deleted. `detections.source` distinguishes them and
nothing in the database says `synthetic`.

**"What is the weakest part?"**
ANPR accuracy, and we have measured it rather than estimated it. Nine
hand-labelled plates is a small sample and we quote it as nine.

**"What would you do with another week?"**
Label 200 plates instead of nine, and put a dedicated ANPR camera at one
chokepoint to show what the pipeline does when the optics are right.

**"What happens if the registry goes down?"**
The control room keeps working. `/api/health` reports it, already-indexed
metadata still searches, and only fresh camera onboarding stops. The reverse is
also true — the registry has no dependency on this service at all.
