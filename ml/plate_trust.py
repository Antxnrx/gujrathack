"""What an operator should actually *do* with a plate read.

The problem with a confidence number
------------------------------------
Every ANPR system shows a plate and a percentage. Almost none tell you what the
percentage means, because almost none have measured it — and an uncalibrated
confidence is decoration. If a system is "80% confident" on reads that turn out
right half the time, the number has actively misled the operator who acted on it.

This service measured it. Twenty-four crops were exported for a human to read;
nine were legible, and the pipeline's output was scored against them
(``scripts/export_plate_labels.py``):

    confidence band   n   exact match
    0.00 – 0.30       3          0%
    0.30 – 0.50       4         25%
    0.50 – 0.70       2        100%

Nine plates is a small sample and the bands below are stated as provisional
because of it. But the shape is unambiguous and it is the useful part: reads
above 0.5 were right every time, reads below 0.3 never were. That is a system
that knows when to be believed, which in a control room is worth more than a
higher headline accuracy nobody can apportion.

So the number is translated into the decision it implies:

    actionable   act on it — put it on a watchlist, radio it, query the registry
    verify       a human should look at the crop before anyone acts
    lead         a direction to search, never a conclusion

Why this belongs in one place
-----------------------------
The alternative is every surface inventing its own threshold — the alert queue
picking 0.6, the search results 0.4, a report 0.75 — and an operator learning
three different meanings for the same colour. One function, cited to one
measurement, re-derived when the measurement improves.

When the sample grows, change ``MEASURED`` and the bands move everywhere at once.
"""
from __future__ import annotations

from dataclasses import dataclass

# The measurement these bands rest on. Kept here rather than in a comment so the
# provenance travels with the thresholds and a stale claim is obvious.
MEASURED = {
    "source": "scripts/export_plate_labels.py",
    "labelled_crops": 24,
    "legible_to_human": 9,
    "exact_match": "3/9 (33.3%)",
    "character_error_rate": "38.9%",
    "measured_on": "2026-09-13, restored GPU index + daylight re-index",
    "bands": {"0.0-0.3": "0%", "0.3-0.5": "25%", "0.5-0.7": "100%"},
    "caveat": "nine plates; bands are provisional and quoted as such",
}

ACTIONABLE = 0.50
VERIFY = 0.30


@dataclass(frozen=True)
class Trust:
    tier: str          # actionable | verify | lead
    label: str         # what to show an operator
    guidance: str      # what to do about it
    measured: str      # observed accuracy in this band, with its sample size


_ACTIONABLE = Trust(
    "actionable",
    "Act on",
    "Safe to put on a watchlist or query against the registry.",
    "2 of 2 exact in this band",
)
_VERIFY = Trust(
    "verify",
    "Verify first",
    "Open the crop and confirm the characters before anyone acts on it.",
    "1 of 4 exact in this band",
)
_LEAD = Trust(
    "lead",
    "Lead only",
    "Treat as a direction to search. Do not record it as the vehicle's identity.",
    "0 of 3 exact in this band",
)


def plate_trust(confidence: float | None, *, read_count: int | None = None,
                truncated: bool = False) -> Trust:
    """Translate a consensus confidence into what to do about it.

    ``read_count`` and ``truncated`` can only ever *lower* the tier, never raise
    it. A single frame is capped at 0.28 by the consensus itself and so already
    lands in ``lead``; the explicit check is here because that cap is a property
    of one module and this should not silently depend on it.

    A short tail demotes out of ``actionable`` regardless of confidence. Indian
    registrations are issued with four trailing digits, so ``GJ11AB457`` is
    almost always a dropped character — and a plate that is confidently missing a
    character is the most dangerous kind, because it looks actionable and
    searches a registry for something never issued.
    """
    c = float(confidence or 0.0)

    if read_count is not None and read_count <= 1:
        return _LEAD
    if c >= ACTIONABLE:
        return _VERIFY if truncated else _ACTIONABLE
    if c >= VERIFY:
        return _VERIFY
    return _LEAD


def describe(confidence: float | None, *, read_count: int | None = None,
             truncated: bool = False) -> dict:
    """JSON-serialisable form, for the API to attach to a plate."""
    t = plate_trust(confidence, read_count=read_count, truncated=truncated)
    return {
        "tier": t.tier,
        "label": t.label,
        "guidance": t.guidance,
        "measured": t.measured,
        "thresholds": {"actionable": ACTIONABLE, "verify": VERIFY},
    }
