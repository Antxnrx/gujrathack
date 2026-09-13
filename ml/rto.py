"""The registration grammar a plate is actually issued under.

``plate_validator`` knows the *shape* of an Indian plate: two letters, one or
two digits, one to three letters, one to four digits. That is the regex, and a
regex is a weak prior — ``GJ99ZZ0000`` satisfies it and does not exist.

This module holds the part that is real-world fact rather than pattern:

    the template   Registrations issued under the 1989 scheme are written
                   ``XX00XX0000`` — state, RTO, series, number, with the
                   character *class* fixed at every position. A ten-character
                   read therefore has exactly one legal parse, and any glyph
                   sitting in the wrong class is an OCR error with a known fix.

    the RTO list   Gujarat has issued codes GJ-01…GJ-40, and GJ-28 and GJ-29
                   have never been allocated. A read of ``GJ28…`` is not an
                   unusual vehicle; it is a misread digit.

Both are cheap, and both do work the image cannot. At 60 px wide the glyphs O,
0, Q and D are the same handful of pixels, and no amount of model quality
separates them — but position does. The sixth character of a ten-character plate
is a *letter*, so a zero read there is an O, decided and not guessed.

The district names are here for the same reason. An operator reading
``GJ11CO5760`` off a screen in Junagadh should be told the vehicle is registered
in Junagadh, because a local registration and one from Kutch mean different
things at a checkpoint, and the plate already carries that fact.

Source for the code list: Wikipedia, "List of Regional Transport Office
districts in India", Gujarat section. New RTOs are created from time to time,
so an unknown code is scored *down* and never rejected outright — this is a
prior, not a gate.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# Gujarat's issued RTO codes
# ─────────────────────────────────────────────────────────────────────────────
GUJARAT_RTO: dict[int, str] = {
    1: "Ahmedabad (West)", 2: "Mehsana", 3: "Rajkot", 4: "Bhavnagar",
    5: "Surat", 6: "Vadodara", 7: "Kheda", 8: "Banaskantha",
    9: "Sabarkantha", 10: "Jamnagar", 11: "Junagadh", 12: "Kutch (West)",
    13: "Surendranagar", 14: "Amreli", 15: "Valsad", 16: "Bharuch",
    17: "Panchmahal", 18: "Gandhinagar", 19: "Surat (East)", 20: "Dahod",
    21: "Navsari", 22: "Narmada", 23: "Anand", 24: "Patan",
    25: "Porbandar", 26: "Tapi", 27: "Ahmedabad (East)",
    # 28 and 29 have never been allocated — see UNALLOCATED below.
    30: "Dang", 31: "Aravalli", 32: "Gir Somnath", 33: "Botad",
    34: "Chhota Udaipur", 35: "Mahisagar", 36: "Morbi",
    37: "Devbhoomi Dwarka", 38: "Ahmedabad (Rural)", 39: "Kutch (East)",
    40: "Vav-Tharad",
}

# Reserved in the sequence but never issued. Worth naming separately from
# "above the highest code": a read of GJ-28 is wrong *today* and will still be
# wrong when GJ-41 exists, whereas GJ-41 is merely unknown to this table.
GUJARAT_UNALLOCATED = frozenset({28, 29})

# The highest code Gujarat has issued. Codes beyond it are treated as unknown
# rather than impossible, because the list grows.
GUJARAT_MAX = max(GUJARAT_RTO)


# ─────────────────────────────────────────────────────────────────────────────
# The template
# ─────────────────────────────────────────────────────────────────────────────
# ``L`` a letter position, ``D`` a digit position. The canonical modern form is
# first because it is overwhelmingly the most common on a live road, and the
# rest are the legal shorter forms: older registrations, single-letter series
# issued before a district exhausted them, and three-letter series issued after.
TEMPLATE = "LLDDLLDDDD"

def _shapes() -> tuple[str, ...]:
    """Every legal registration shape, most typical first.

    Enumerated rather than hand-listed: two state letters, a one- or two-digit
    RTO, a one- to three-letter series, and a one- to four-digit number is 24
    templates, and a hand-written subset silently drops real ones — ``GJ 11 ABC
    457`` was missing from the first version of this list, so every plate of
    that shape failed to parse at all.

    The order is the prior: two-digit RTO before one, two-letter series before
    one before three, four-digit number before the shorter tails.
    """
    out: list[str] = []
    for rto_len in (2, 1):
        for series_len in (2, 1, 3):
            for num_len in (4, 3, 2, 1):
                out.append("LL" + "D" * rto_len + "L" * series_len + "D" * num_len)
    return tuple(out)


SHAPES: tuple[str, ...] = _shapes()

# What a glyph becomes when it is sitting in a position of the wrong class.
# Both directions of the same visual confusions: a digit seen where a letter
# belongs, and a letter seen where a digit belongs.
TO_LETTER = {
    "0": "O", "1": "I", "2": "Z", "4": "A", "5": "S",
    "6": "G", "7": "T", "8": "B", "9": "G",
}
TO_DIGIT = {
    "O": "0", "Q": "0", "D": "0", "U": "0",
    "I": "1", "L": "1", "J": "1", "T": "7",
    "Z": "2", "A": "4", "S": "5", "G": "6", "B": "8",
}

_CLEAN = re.compile(r"[^A-Z0-9]")


def clean(text: str | None) -> str:
    """Upper-case, strip everything that is not a plate character."""
    return _CLEAN.sub("", (text or "").upper())


def coerce(text: str, shape: str) -> str | None:
    """Force a read onto one template, fixing glyphs that sit in the wrong class.

    Returns ``None`` when the read is the wrong length for the shape, or when a
    character cannot be moved into the required class at all — ``X`` in a digit
    position has no plausible digit reading, and inventing one would be worse
    than failing.
    """
    if len(text) != len(shape):
        return None
    out: list[str] = []
    for ch, kind in zip(text, shape, strict=True):   # length checked above
        if kind == "D":
            if ch.isdigit():
                out.append(ch)
            elif ch in TO_DIGIT:
                out.append(TO_DIGIT[ch])
            else:
                return None
        else:
            if ch.isalpha():
                out.append(ch)
            elif ch in TO_LETTER:
                out.append(TO_LETTER[ch])
            else:
                return None
    return "".join(out)


def edits(a: str, b: str) -> int:
    """How many characters coercion had to change.

    Positional, not Levenshtein: ``coerce`` never changes length, so the two
    strings line up and a substitution is the only edit possible.
    """
    return sum(x != y for x, y in zip(a, b, strict=True))


# ─────────────────────────────────────────────────────────────────────────────
# Is this a registration that exists?
# ─────────────────────────────────────────────────────────────────────────────
# The floor on any plausibility penalty, and the reason for it.
#
# A parse that rewrites one character costs a factor of 0.5 in
# plate_validator.correct_plate. So a plausibility below 0.5 is worth *more*
# than a character of evidence, and the grammar starts rewriting glyphs it can
# see in order to reach a district it likes. That is not a hypothetical:
# GJ-28 is unallocated, and at a penalty of 0.15 the validator turned the
# legibly-read ``GJ28AB1234`` into ``GJ2BAB1234`` — inventing a three-letter
# series to escape a code it disbelieved.
#
# A prior about the world must never outbid a character actually read. The
# right job for this signal is to break ties between parses that changed the
# same number of glyphs, and to mark the result as suspect for the operator —
# both of which it still does at 0.6.
_IMPLAUSIBLE = 0.6


def rto_plausibility(state: str, rto: str) -> float:
    """How likely is this state/RTO pair to be a real registering authority, 0–1.

    Only Gujarat is enumerated, because Gujarat is the jurisdiction this system
    polices and a wrong list is worse than no list. Everything else gets a
    neutral score: an out-of-state plate is perfectly ordinary on a Gujarat
    highway and must not be penalised for being unverifiable here.
    """
    try:
        code = int(rto)
    except (TypeError, ValueError):
        return 0.0
    if code == 0:
        return 0.0                       # no RTO is numbered 00, in any state
    if state != "GJ":
        return 0.75                      # plausible, simply not checkable here
    if code in GUJARAT_RTO:
        return 1.0
    return _IMPLAUSIBLE                  # unallocated, or issued since this list


def is_known_rto(plate: str | None) -> bool:
    """Does this plate name a registering authority that exists?

    False for a Gujarat code that has never been allocated (GJ-28, GJ-29) or
    one past the end of the list. True for every other state, which this module
    does not enumerate and must not pretend to judge.
    """
    m = re.match(r"^([A-Z]{2})(\d{1,2})", clean(plate))
    if not m:
        return False
    if m.group(1) != "GJ":
        return True
    return int(m.group(2)) in GUJARAT_RTO


def district(plate: str | None) -> str | None:
    """The registering district, for a plate this module can place.

    ``GJ11CO5760`` → ``"Junagadh"``. Returns ``None`` for another state or a
    code Gujarat has not issued, so the caller can stay silent rather than
    guess.
    """
    m = re.match(r"^([A-Z]{2})(\d{1,2})", clean(plate))
    if not m or m.group(1) != "GJ":
        return None
    return GUJARAT_RTO.get(int(m.group(2)))


def describe(plate: str | None) -> str | None:
    """A one-line provenance for the console: ``"GJ 11 · Junagadh"``."""
    d = district(plate)
    if not d:
        return None
    m = re.match(r"^([A-Z]{2})(\d{1,2})", clean(plate))
    assert m is not None                 # district() already matched
    return f"{m.group(1)} {int(m.group(2)):02d} · {d}"


# ─────────────────────────────────────────────────────────────────────────────
# The OCR's own admission of defeat
# ─────────────────────────────────────────────────────────────────────────────
# The India-specific OCR emits the literal word UNREADABLE on crops it cannot
# resolve — an artefact of how its training set labelled illegible plates. It
# arrived here as twenty "registrations" reading UNREADBLE, UNRADBLE, UNREADBL3
# across ten different cameras, presented to operators as vehicle identities.
#
# It is the single most useful thing this model says, because it is the only
# case where it declines to guess: asked to read solid black it returns
# "A22251" at 0.23 confidence, and asked to read noise it returns "ALEE" at
# 0.49. There is no null output. This is the nearest thing to one.
_ABSTENTION = "UNREADABLE"


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j - 1] + (ca != cb), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def is_abstention(text: str | None) -> bool:
    """True when the OCR is saying it could not read the crop.

    Matched with tolerance, because the model misspells its own refusal — the
    observed forms run from ``UNREADABLE`` through ``UNREADBL3`` to
    ``UNUNREADBE``. Three edits admits every form seen here and is still far
    from any legal plate: the closest real registration to ``UNREADABLE`` in
    this estate is eight edits away.
    """
    t = clean(text)
    if len(t) < 6:
        return False
    return _distance(t, _ABSTENTION) <= 3 or "UNREAD" in t or "UNRAD" in t
