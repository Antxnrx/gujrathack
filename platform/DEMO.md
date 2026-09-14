# Demo script

Fifteen minutes, five acts. Every figure below is live from the running system —
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

**Dashboard → the counters.** 30 cameras, 61,982 detections, 1,913 tracks.

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

## Act 3 — A plate, and why you should believe it (5 minutes)

This is the act that wins or loses the room. Do not rush it.

**Search → plate `GJ11BR`.** One track returns: a white truck at cam06
Timbavadi gate, `GJ11BR2912`, 10 reads.

Open it and show the **per-character vote**.

> "Most systems show you a plate and a percentage. A percentage cannot tell you
> whether ten frames agreed on every character or two frames agreed on six — both
> round to the same number and they are not the same evidence.
>
> So we show the ballot. Each column is one character position, and the bars are
> what the frames voted. An amber column is where the runner-up was nearly as
> well supported — that is precisely where the plate is most likely wrong, and it
> is the column an operator should search both ways."

### The thing we found by looking

> "Early on, an officer looking at this dashboard told us the plates were wrong —
> that he could read them with his own eyes and the machine could not. He was
> right, and the reason turned out to be worse than inaccuracy.
>
> This OCR has **no null output**. Ask it to read solid black and it answers
> `A22251`. Ask it to read pure noise and it answers `ALEE` at 0.49 confidence.
> Every crop produces a string, always.
>
> So thirty-four of a hundred and eleven vehicles here carried a 'registration'
> that is not one — and twenty of those were the word **UNREADABLE**, misspelled.
> The model was telling us it could not read the crop, and we were storing its
> confession in the plate column and showing it to officers as a vehicle's
> identity."

Show one: a real car at cam06, plate blurred beyond reading, registered
`UNREADBLE`.

> "That is the worst failure a system like this has. Not being wrong — being
> wrong in a way the user cannot detect."

### The fix is the plate itself

> "An Indian registration is issued as `XX00XX0000`. The character *class* is
> fixed at every position, so a ten-character read has exactly one legal parse —
> the sixth character is a letter, so a zero read there is an O. Decided, not
> guessed. And Gujarat has issued GJ-01 to GJ-40 with GJ-28 and GJ-29 never
> allocated, so a read naming one of those is a misread digit rather than a rare
> vehicle."

| | before | after |
|---|---|---|
| tracks shown with a plate that is not a registration | **34** | **0** |
| of those shown, naming an RTO that exists | — | **77 / 77** |

> "And the grammar is not allowed to run away with it. Two real reads pin how
> far the prior may go, and they pull in opposite directions: on `GJ11AB457` the
> prior wanted to rewrite a legibly-read B into an 8 to manufacture the four-digit
> tail it expects — the prior must not buy a character it did not see. On
> `GJ1IC05532` the cheapest parse by edit count is the wrong one, so edit count
> must not dominate either. Both are in the test suite."

### The headline number, including where it went down

> "We magnified forty-eight plate regions and read them by eye. Fifteen were
> legible, twenty-six were plates too degraded for a human, and seven were not
> plates at all."

| | 9 plates | 15 plates | **19 plates** |
|---|---|---|---|
| exact match | 78% | 67% | **68%** |
| character error rate | 13.3% | 10.1% | **4.8%** |

> "The number went **down** between the first two columns, because the bigger set
> includes harder cases the first pass had skipped. We would rather correct that
> ourselves than have you find it.
>
> The last column is a different kind of fix. Character error more than halved
> while exact match barely moved, and that gap is the whole story: the reader did
> not get better at characters. It stopped attributing one car's plate to another
> car — which is what the next slide is about."

Then the two things that make 67% workable:

> "**Four of the five misses are one character out.** `GJ15CB2936` read as
> `GJ15CB2934`. `GJ11CO5760` read as `GJ11CQ5760` — a Q whose tail is about one
> pixel at this resolution. No model separates those.
>
> So search does not use string equality. It matches on confusion distance, and
> scores that pair at **0.975** — above the alerting threshold. The wanted vehicle
> is still found; only the displayed string is wrong. I will show you that in a
> moment.
>
> And **the confidence predicts correctness**, which matters more than the
> headline:"

| band | n | exact |
|---|---|---|
| actionable | 7 | 71% |
| verify | 7 | 71% |
| lead | 1 | **0%** |

> "An operator is told which plates to act on. A system with higher raw accuracy
> and no calibration is worse in a control room, because nothing distinguishes
> the good reads from the bad."

If asked *why only 67%* — that is Act 4, and it is a good question to be asked.

---

## Act 4 — Where it works, and where it cannot (3 minutes)

**Cameras → sort by ANPR capability.**

> "We measured every camera on what it actually delivered. Seven of thirty read
> plates reliably and seven occasionally: cam09 at 22%, then a gate, a toll plaza,
> a bus port and an approach road. Every one is a chokepoint — somewhere vehicles
> slow down and pass close to the lens."

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
> and this estate already has seven of them. So the system says which cameras can
> answer a plate question and which cannot, instead of silently returning
> nothing."

**Close on Evidence.** Seal a snapshot, then verify it. (Case work starts empty
by design — nothing in this database describes work nobody did. Act 5's Wanted
vehicle playbook creates the incident and the pack live, in about a tenth of a
second.)

> "Two separate facts: the seal is intact, and the live data has drifted. An old
> snapshot legitimately drifts. Collapsing those into one valid/invalid light is
> how you train an operator to ignore the one that matters."

---

## Act 5 — Doing the job (4 minutes)

**Sidebar → Do a job.** Four cards, each a question rather than a table.

> "Everything you have seen so far is a capability. This is the part that makes
> it a product. An officer does not arrive wanting to browse detections — they
> arrive with a job: a vehicle was stolen, something happened outside a shop at
> eleven, this is going to court.
>
> Each of these runs a fixed sequence over five or six endpoints, in an order
> that matters. Left in the operator's head, that sequence is performed
> differently by every officer and never twice the same way in a case file."

### The demonstration to lead with

**Wanted vehicle → `GJ11CO5760`**, reason *"Stolen vehicle — FIR 221/2026"*.

> "I am typing the plate as it is on the vehicle. The index does not contain that
> string — our reader recorded it as `GJ11CQ5760`, one glyph out.
>
> Watch what happens."

Two sightings come back at cam09, marked **inferred 0.975**, not *exact*.

> "Before this, the retrospective search was an exact string match, and this
> query returned nothing. The officer would have been told a stolen vehicle had
> never been seen — which is the one wrong answer indistinguishable from the
> truth.
>
> And it does not hide the inference. The badge says these matched one
> confusion-class character away, so the officer confirms against the crop before
> dispatching a unit."

Point at the sidebar: the watchlist is armed, two alerts are raised, an incident
is open with the events already attached.

> "Arming the future and searching the past are the two halves of one question,
> and they used to be two separate operations. For a vehicle reported stolen
> hours after the fact, the past half is the half that matters."

### The part to dwell on

**Scene canvass → cam06 and cam09, one hour.** Read the caveats panel out loud.

> "It found twenty-one vehicles and four people. Thirteen have a plate. One is in
> the band you can act on directly.
>
> And then this —"

> **"1 registration appears on more than one kind of vehicle in this window,
> which cannot be true of a real plate — `GJ11CQ5760` reads as white car and red
> truck."**

> "A registration is unique to a vehicle. The same plate on a car and a truck
> eighty-five seconds apart means at least one of those reads is wrong.
>
> Nothing else in the system is placed to notice. Each read is individually
> plausible, each passes the grammar, each carries a reasonable confidence — it
> is only together, in one list, that they are impossible. An officer who spots
> that themselves stops trusting the whole canvass. So we say it first, above the
> results, before anything gets acted on."

Note the ordering:

> "This list is not chronological. It is ordered by how far each plate can be
> trusted, so the vehicles worth acting on are at the top and the officer can
> stop reading. Vehicles with no readable plate are kept, last — a make, a colour
> and a time is still what a witness statement is made of."

And the estate caveat, every time:

> "Seven of thirty cameras read plates reliably. At the other sixteen a vehicle
> can pass and be seen without its registration ever being readable. So 'no plate
> hit' is not 'no vehicle', and the system says so rather than letting silence be
> read as absence."

### Close on the seal

**Seal for court** on that incident. It seals, then immediately verifies.

> "Verifying straight after sealing looks redundant and is not. It proves the
> manifest stored is the manifest hashed, now, while a mismatch is still a bug
> someone can fix — rather than six months later in a courtroom, where the same
> mismatch is indistinguishable from tampering.
>
> Two separate facts: the seal is intact, and the live data has drifted. An old
> snapshot legitimately drifts. Collapsing those into one valid/invalid light is
> how you train an operator to ignore the one that matters."

### If someone asks about roles

Run the canvass again signed in as the analyst with **Open a case file** ticked.

> "Reading who was present is a read, and the analyst gets it. Opening a case
> file is a write, and they do not — and the message says exactly that instead of
> a bare 403. The role model is real, and it is drawn at the operation rather
> than at the page."

---

## The live pipeline, run in front of them (2 minutes)

Worth doing last, because it answers the question a judge will ask: *is any of
this live, or is it a database of old results?*

```bash
CAMERAS=cam06 .venv/bin/python -m ml.worker   # the inference worker, one camera
tail -f /tmp/setu-analytics.log          # the fold, already running
```

> "Nothing is pre-computed here. The worker signs in to the gateway, pulls the
> stream, and runs YOLO, CLIP and the plate reader on every fifth frame. Watch
> the analytics log — it has been idle all evening."

Measured on the run that produced this page:

| | |
|---|---|
| frames processed | 100 in ~60 s |
| detections written | **572** |
| tracks folded | **24** |
| plates read | 2 — `GJ32AG2883`, `GJ32AG2983` |
| events raised | 4 zone intrusions |

> "The fold picked those up on its own, sixty seconds later, and the console
> updated without a refresh. Nobody ran anything between the camera and the
> screen."

```
23:41:53  6 track(s) folded from 55 detection(s) · 1 new event(s)
23:42:55  23 track(s) folded from 348 detection(s) · 3 new event(s)
```

**Say the caveat before they find it.** The gateway serves
`#EXT-X-PLAYLIST-TYPE:VOD` — a twelve-hour recording, not a live edge. So this is
live *ingestion* of a streaming source, not a camera filming now. The code path
is the same one a live camera uses; what changes on the day real feeds arrive is
the playlist, and the player already reads the playlist and will say `live`
instead of `recorded` by itself.

---

## Questions you should want

**"Is any of this synthetic?"**
No. The database was rebuilt from the GPU run's own output; the synthetic seed
the project shipped with was deleted. `detections.source` distinguishes them and
nothing in the database says `synthetic`.

**"What is the weakest part?"**
ANPR accuracy, and we have measured it rather than estimated it. Fifteen
hand-read plates is a small sample and we quote it as fifteen — and we report
that the number fell from 78% to 67% when the sample grew, because that is what
happened.

**"How do you know a plate on screen is not made up?"**
Because it has to be a registration that could be issued. The grammar rejects
what is not, and the RTO code has to be one Gujarat has actually allocated —
thirty-four vehicles here lost their fake plate to that check, twenty of them
reading `UNREADABLE` in the model's own hand. What is left keeps its confidence
tier on screen, so an unverified read never looks like a confirmed one.

**"Your reader is 67% accurate — how is that usable?"**
Because the errors are one glyph, and nothing that matters uses string equality.
Search and the watchlist match on confusion distance: the correct plate finds
the misread one at 0.975. And the canvass tells you when it has contradicted
itself. The honest claim is not "we read plates perfectly"; it is "we find the
vehicle anyway, and we tell you how sure we are."

**"What would you do with another week?"**
Label 200 plates instead of fifteen, and put a dedicated ANPR camera at one
chokepoint to show what the pipeline does when the optics are right.

**"What happens if the registry goes down?"**
The control room keeps working. `/api/health` reports it, already-indexed
metadata still searches, and only fresh camera onboarding stops. The reverse is
also true — the registry has no dependency on this service at all.
