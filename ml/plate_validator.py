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

# All valid Indian state/UT codes
INDIAN_STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA",
    "GJ", "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH",
    "ML", "MN", "MP", "MZ", "NL", "OD", "OR", "PB", "PY", "RJ",
    "SK", "TN", "TR", "TS", "UK", "UP", "WB",
}

# Common OCR character confusions: what the OCR reads → what it should be
# Context-dependent: letters in letter positions, digits in digit positions
_LETTER_FIXES = {
    "0": "O", "1": "I", "2": "Z", "5": "S", "8": "B",
}
_DIGIT_FIXES = {
    "O": "0", "I": "1", "Z": "2", "S": "5", "B": "8",
    "D": "0", "G": "6", "T": "7", "A": "4", "Q": "0",
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


def _fix_char(ch: str, expect_digit: bool) -> str:
    """Apply contextual OCR correction for a single character."""
    if expect_digit:
        return _DIGIT_FIXES.get(ch, ch)
    else:
        return _LETTER_FIXES.get(ch, ch)


def correct_plate(raw: str) -> str | None:
    """
    Try to correct common OCR errors in an Indian plate string.

    Applies context-dependent character fixes based on where letters vs digits
    are expected in the Indian plate format, then validates against the pattern.

    Returns the corrected plate string if it matches, or None if unrecoverable.
    """
    if not raw or len(raw) < 6:
        return None

    text = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(text) < 6:
        return None

    # Strategy: try multiple interpretations of where the letter/digit
    # boundaries fall and see which one produces a valid plate.

    best = None

    for district_len in (1, 2):
        for series_len in (1, 2, 3):
            # State is always first 2 chars
            min_len = 2 + district_len + series_len + 1  # at least 1 digit at end
            if len(text) < min_len:
                continue

            state_end = 2
            district_end = state_end + district_len
            series_end = district_end + series_len

            # Apply contextual corrections
            corrected = []
            for i, ch in enumerate(text):
                if i < state_end:
                    corrected.append(_fix_char(ch, expect_digit=False))
                elif i < district_end:
                    corrected.append(_fix_char(ch, expect_digit=True))
                elif i < series_end:
                    corrected.append(_fix_char(ch, expect_digit=False))
                else:
                    corrected.append(_fix_char(ch, expect_digit=True))

            candidate = "".join(corrected)
            m = _PLATE_PATTERN.match(candidate)
            if m:
                state = m.group(1)
                # If state code isn't valid, try common OCR corrections on it
                if state not in INDIAN_STATE_CODES:
                    # Try fixing each char in the state code using letter alternatives
                    s0, s1 = state[0], state[1]
                    alts0 = [s0] + _STATE_LETTER_ALTS.get(s0, []) + [_LETTER_FIXES.get(s0, s0)]
                    alts1 = [s1] + _STATE_LETTER_ALTS.get(s1, []) + [_LETTER_FIXES.get(s1, s1)]
                    found = False
                    for c0 in alts0:
                        for c1 in alts1:
                            if c0 + c1 in INDIAN_STATE_CODES:
                                candidate = c0 + c1 + candidate[2:]
                                state = c0 + c1
                                found = True
                                break
                        if found:
                            break

                if state in INDIAN_STATE_CODES:
                    score = len(m.group(4))
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

    _state, rto, series, number = m.groups()

    # The registration number. Four digits is the issued form; anything shorter
    # is either an old plate or, far more often here, a dropped character.
    tail = {4: 1.0, 3: 0.45, 2: 0.30, 1: 0.20}.get(len(number), 0.2)

    # RTO codes are written with two digits on the plate (GJ 01, not GJ 1).
    rto_score = 1.0 if len(rto) == 2 else 0.6

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
    """
    Validate and optionally correct an Indian license plate.

    Returns:
        The cleaned/corrected plate if valid, or None if it doesn't match
        the Indian plate format even after correction attempts.
    """
    if not raw:
        return None

    text = re.sub(r"[^A-Z0-9]", "", raw.upper())

    # First check if it already matches perfectly
    m = _PLATE_PATTERN.match(text)
    if m and m.group(1) in INDIAN_STATE_CODES:
        return text

    # Try correction
    return correct_plate(text)
