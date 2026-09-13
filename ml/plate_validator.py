"""
Indian license plate validation and correction.

Indian plate format:  XX 00 XX 0000
  - 2 letters:  State code (GJ, MH, DL, RJ, etc.)
  - 1-2 digits: District/RTO code (1-99)
  - 1-2 letters: Series code (A-Z, AA-ZZ)
  - 1-4 digits: Registration number (1-9999)

Example: GJ 14 DX 5823, MH 02 AB 1234, DL 8 C 4567
"""

from __future__ import annotations

import re

from ml import rto

# All valid Indian state/UT codes
INDIAN_STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA",
    "GJ", "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH",
    "ML", "MN", "MP", "MZ", "NL", "OD", "OR", "PB", "PY", "RJ",
    "SK", "TN", "TR", "TS", "UK", "UP", "WB",
}

# Common OCR character confusions: what the OCR reads → what it should be
# Context-dependent: letters in letter positions, digits in digit positions
# Positional coercion now lives in ml/rto.py (TO_LETTER / TO_DIGIT), which
# applies it against the registration template rather than a guessed split.
# What remains here is the state-code repair, which is a different problem: the
# first two characters are letters under every template, so class coercion
# never helps and only a letter→letter substitution can rescue them.
_LETTER_FIXES = {
    "0": "O", "1": "I", "2": "Z", "5": "S", "8": "B",
}

# For state code correction: letter→letter confusions that produce wrong state codes
# e.g. GI is not a state, but GJ is (I↔J confusion is very common)
_STATE_LETTER_ALTS = {
    "I": ["J", "L"],  # I↔J (most common), I↔L
    "J": ["I"],        # J↔I
    "U": ["V"],        # U↔V
    "V": ["U"],
    "C": ["G"],        # C↔G
    "G": ["C"],
    "E": ["F"],        # E↔F
    "F": ["E"],
}

# Regex for a valid Indian plate after cleaning
# State(2) + District(1-2 digits) + Series(1-2 letters) + Number(1-4 digits)
_PLATE_PATTERN = re.compile(
    r"^([A-Z]{2})"       # State code
    r"(\d{1,2})"         # District/RTO
    r"([A-Z]{1,3})"      # Series (some states use 3 letters)
    r"(\d{1,4})$"        # Registration number
)


def _repair_state(candidate: str) -> str | None:
    """Nudge an unrecognised state code onto a real one, or give up.

    Only the letter↔letter confusions that actually produce state codes are
    tried (``GI`` → ``GJ``), plus the digit→letter fixes. A code that is not a
    state after that is left alone, because inventing a state is how a plate
    from nowhere acquires a jurisdiction.
    """
    state = candidate[:2]
    if state in INDIAN_STATE_CODES:
        return candidate
    s0, s1 = state[0], state[1]
    alts0 = [s0] + _STATE_LETTER_ALTS.get(s0, []) + [_LETTER_FIXES.get(s0, s0)]
    alts1 = [s1] + _STATE_LETTER_ALTS.get(s1, []) + [_LETTER_FIXES.get(s1, s1)]
    for c0 in alts0:
        for c1 in alts1:
            if c0 + c1 in INDIAN_STATE_CODES:
                return c0 + c1 + candidate[2:]
    return None


# Each character the template had to rewrite halves the parse's score. See
# correct_plate's docstring for the two real reads that pin this value.
_EDIT_PENALTY = 0.5


def correct_plate(raw: str) -> str | None:
    """Try to correct common OCR errors in an Indian plate string.

    The read is fitted to each registration template in ``ml.rto.SHAPES`` —
    ``XX00XX0000`` and the legal shorter forms — with every glyph coerced to the
    character class its position requires. That is where most of the recovery
    happens, and it is recovery rather than guessing: the sixth character of a
    ten-character plate is a letter by construction, so a ``0`` read there is an
    ``O``, and a ``Q`` that the model produced for the same round glyph is
    resolved the same way.

    Candidates are then scored rather than taken first-fit, on three factors:

        format typicality   a four-digit tail is the issued form and a two-digit
                            RTO is how districts are written; the shorter forms
                            are real but much rarer
        RTO plausibility    ``GJ28`` has never been allocated, so a read that
                            parses to it is a misread digit, not a rare vehicle
        edit conservatism   every character the template had to rewrite halves
                            the parse's score

    The third is what keeps the first two honest, and both directions of getting
    it wrong showed up on real reads here:

        GJ11AB457    a three-digit tail, so the typicality prior alone preferred
                     rewriting the legibly-read ``B`` into an ``8`` to
                     manufacture ``GJ11A8457``. The prior must not be allowed to
                     buy a character it did not see.
        GJ1IC05532   the cheapest parse by edit count is ``GJ1ICO5532`` — one
                     rewrite, reading it as RTO 1 with a three-letter series.
                     The truth is ``GJ11CO5532``, two rewrites onto the
                     canonical template. So edit count must not dominate
                     absolutely either.

    Between them those two pin the penalty to roughly 0.42–0.56 per character;
    0.5 sits in the middle and is what a halving means. Both cases are in the
    test suite, because this is a number that a later tweak would otherwise
    silently break.

    Returns the corrected plate, or ``None`` when nothing legal survives.
    """
    text = rto.clean(raw)
    if len(text) < 6:
        return None
    if rto.is_abstention(text):
        return None

    best: tuple[str, float] | None = None

    for shape in rto.SHAPES:
        coerced = rto.coerce(text, shape)
        if coerced is None:
            continue
        candidate = _repair_state(coerced)
        if candidate is None:
            continue
        m = _PLATE_PATTERN.match(candidate)
        if not m or m.group(1) not in INDIAN_STATE_CODES:
            continue

        # The state repair is an edit too, and an expensive one — it rewrites
        # the jurisdiction — so it is counted rather than waved through.
        changed = rto.edits(text, coerced) + (0 if candidate == coerced else 1)
        score = (format_typicality(candidate)
                 * rto.rto_plausibility(m.group(1), m.group(2))
                 * (_EDIT_PENALTY ** changed))
        if score <= 0:
            continue
        if best is None or score > best[1]:
            best = (candidate, score)

    return best[0] if best else None


def matches_format(text: str) -> bool:
    """Strict format predicate — is this string already a legal plate?

    No correction, no fuzz. ``validate_plate`` both validates and repairs, which
    is right for a single OCR read but wrong when a caller is scoring candidate
    strings of its own and needs to know which ones are legal as-is.
    Used by ml/analytics/anpr.py's multi-frame consensus snapping.
    """
    if not text:
        return False
    m = _PLATE_PATTERN.match(text)
    return bool(m) and m.group(1) in INDIAN_STATE_CODES


def format_typicality(text: str) -> float:
    """How *likely* this shape is for a plate in current issue, 0–1.

    ``matches_format`` answers whether a string is legal. Legality is not
    likelihood, and conflating them is what lets a short read pass as a finished
    answer. The regex admits a 1-digit tail because ``GJ1A1`` is a real format —
    but under the 1989 scheme the registration number is issued as **four
    digits**, and the shorter tails are almost entirely older or special-series
    registrations. On a modern road scene, a 3-digit tail is far more often an
    OCR that dropped a character than a genuinely short plate.

    So a read of ``GJ11AB457`` should not be treated as a complete plate. It is
    a 9-character answer to a 10-character question, and the missing character is
    usually in the tail where the digits run together.

    This is a prior, not a filter: the score is *returned*, and the caller
    decides. ``ml/analytics/anpr.py`` uses it to break ties in the consensus beam
    search, so that a candidate with a full tail wins over an equally-supported
    truncation, and the UI uses it to mark a read as partial rather than final.

    Weights are ordinal, not fitted — they encode "much more common", not a
    measured frequency, and they should be replaced by real RTO-issue
    distributions if those become available.
    """
    if not text:
        return 0.0
    m = _PLATE_PATTERN.match(text)
    if not m or m.group(1) not in INDIAN_STATE_CODES:
        return 0.0

    _state, rto_code, series, number = m.groups()

    # The registration number. Four digits is the issued form; anything shorter
    # is either an old plate or, far more often here, a dropped character.
    tail = {4: 1.0, 3: 0.45, 2: 0.30, 1: 0.20}.get(len(number), 0.2)

    # RTO codes are written with two digits on the plate (GJ 01, not GJ 1).
    rto_score = 1.0 if len(rto_code) == 2 else 0.6

    # Series letters: two is the modern norm, one is common on older plates,
    # three appears once a district exhausts two-letter series.
    series_score = {2: 1.0, 1: 0.8, 3: 0.7}.get(len(series), 0.5)

    return round(tail * rto_score * series_score, 4)


def is_probably_truncated(text: str) -> bool:
    """True when a read is legal but short enough to be a dropped character.

    Used to mark a plate as *partial* in the console rather than presenting it as
    a finished read. An operator acting on ``GJ11AB457`` should know that the
    system believes a character is missing, because searching a registry for the
    wrong string returns confidently nothing.
    """
    m = _PLATE_PATTERN.match(text or "")
    if not m or m.group(1) not in INDIAN_STATE_CODES:
        return False
    return len(m.group(4)) < 4


def validate_plate(raw: str) -> str | None:
    """Validate and, where the grammar allows, correct an Indian plate.

    Returns the cleaned plate, or ``None`` when the read is not a registration
    — including when the OCR has said so itself (see ``rto.is_abstention``).
    ``None`` is a real answer here and callers must treat it as one: storing an
    unvalidated read as a vehicle's plate is how twenty cars in this estate came
    to be registered ``UNREADBLE``.
    """
    if not raw:
        return None

    text = rto.clean(raw)
    if rto.is_abstention(text):
        return None

    # A read that is already a legal plate is kept as read. Correction is still
    # attempted, because a legal parse is not always the best one — but it has
    # to beat what the OCR actually saw, and ties go to the read.
    m = _PLATE_PATTERN.match(text)
    already_legal = bool(m) and m.group(1) in INDIAN_STATE_CODES

    corrected = correct_plate(text)
    if already_legal:
        return corrected or text
    return corrected
