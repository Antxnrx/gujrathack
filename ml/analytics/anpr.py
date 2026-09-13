"""Multi-frame ANPR — consensus plate reading across a whole track.

Why this exists
---------------
``ml/pipeline.py`` OCRs each frame independently, so a track seen in 11 frames
produces 11 unrelated plate strings. On the Sentinel footage those individual
reads are mostly wrong: the plates are small, motion-blurred and often
interlaced, and a single read of a 10-character string has to get all ten right.

But the *errors* are not random. OCR fails in a structured way — it confuses 0
with O, 1 with I, 8 with B — and it fails differently on different frames,
because the blur direction and the plate's angle change. So eleven bad reads of
the same plate carry far more information than the best single read, and
recovering it is a voting problem, not a better-OCR problem.

Three things make the vote work where naive majority voting does not:

1. **Alignment.** Reads differ in length (a dropped character shifts everything
   after it). Voting position-by-position on unaligned strings is worse than
   useless. Off-length reads are aligned to the modal-length cohort with an
   edit-distance backtrace before they contribute.

2. **Confusion-class credit.** A vote for ``0`` also partially supports ``O``,
   ``D`` and ``Q``. Reads of ``0``, ``O``, ``O``, ``D``, ``0`` disagree on the
   character but *agree on the glyph*, and that agreement is the signal. The
   position resolves to whichever member of the class the plate format expects.

3. **Format prior.** Indian plates are ``LL DD L{1,3} DDDD``. Position 4 must be
   a letter, so a ``4`` there is an ``A``. ``ml.plate_validator`` already knows
   this; consensus runs through it at the end.

Confidence is deliberately conservative: it multiplies the per-position vote
margin by a saturating function of the read count, so one read can never look
like proof. One read tops out at 0.28; eight agreeing reads reach ~0.93.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from ml import rto
from ml.plate_validator import format_typicality, matches_format, validate_plate

# ── Glyph confusion classes ──────────────────────────────────────────────────
# Characters within a class are routinely swapped by plate OCR. Membership is
# symmetric and transitive within a class, which is why they are stored as sets
# rather than a pair table.
_CONFUSION_CLASSES: list[set[str]] = [
    {"0", "O", "D", "Q"},
    {"1", "I", "L", "T"},
    {"2", "Z"},
    {"5", "S"},
    {"8", "B"},
    {"6", "G"},
    {"4", "A"},
    {"7", "T"},
    {"9", "P"},
    {"U", "V"},
    {"C", "G"},
    {"E", "F"},
    {"M", "N", "H"},
]

_CONFUSABLE: dict[str, set[str]] = defaultdict(set)
for _cls in _CONFUSION_CLASSES:
    for _ch in _cls:
        _CONFUSABLE[_ch] |= _cls - {_ch}

# Partial credit a vote gives to other members of its glyph class.
_CONFUSION_CREDIT = 0.35
# Weight discount applied to reads that had to be re-aligned to fit the frame.
_MISALIGNED_WEIGHT = 0.6
# Reads shorter than this are noise, not a plate.
_MIN_READ_LEN = 5

# ── Grammar-aware snapping ───────────────────────────────────────────────────
# The per-position argmax is not always a legal plate, and the character that
# makes it legal is often the runner-up by a hair (Z at 3.68 vs 2 at 3.50). The
# validator can't see that — it only gets the final string — so it "corrects"
# whichever character is cheapest for *it*, which can be the wrong one.
#
# So instead of trusting the argmax and asking the validator to repair it, we
# search the top few candidates per position and take the highest-scoring
# combination that is a legal plate outright. Budget is in nats of summed
# log(vote share): roughly, you may trade one character for a plausible
# runner-up, or two for near-ties, but you may not assemble a plate out of noise.
# How much a plate's confidence is discounted for the amount of *unsupported*
# repair its resolution needed. These rank the four paths out of ``_snap`` by how
# far each strays from what the vote actually supports, which is a structural
# ordering, not a fitted one:
#
#   argmax           the vote's own answer, legal as it stands — nothing invented
#   beam_snap        traded vote score for legality, and we know the cost in nats
#   validator_fuzzy  substituted characters the vote never supported at all
#   unsnapped        failed every structural check; not a legal plate in any form
#
# A scorer run against 3,188 seeded tracks measured those paths at
# 90.7% / 65.7% / 25.0% / 0.0% exact, so the ordering is the right one and the
# earlier single "0.75 if not format_valid" was badly wrong in both directions:
# it discounted a legal beam-snap not at all, and paid a 0.75 bonus to unsnapped
# strings that were never once correct. An unsnapped read is still returned — an
# operator may recognise a partial — but it must not present as evidence.
_METHOD_TRUST: dict[str, float] = {
    "argmax": 1.0,
    "beam_snap": 0.85,
    "validator_fuzzy": 0.55,
    "unsnapped": 0.30,
    "single_read": 1.0,     # already hard-capped at 0.28 before it reaches here
}

_SNAP_BEAM_WIDTH = 12
_SNAP_TOP_K = 3
_MAX_SNAP_COST = 2.5
# How much a typical plate shape may outweigh raw vote score, in nats. Sized to
# settle near-ties in favour of a full four-digit tail without letting the prior
# overrule characters the vote actually saw.
_TYPICALITY_WEIGHT = 0.8


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _align(read: str, frame_len: int) -> list[str | None]:
    """Align *read* to a slot list of length *frame_len*.

    Standard Levenshtein DP with a backtrace, biased so that substitutions
    inside a glyph confusion class are nearly free (cost 0.25). That bias
    matters: it stops the aligner from "explaining" an O→0 confusion as a
    delete-plus-insert, which would shift every following character by one and
    destroy the alignment for the rest of the string.

    Returns a list of length *frame_len* where each slot holds the character
    from *read* aligned to it, or ``None`` if the read has nothing there.
    """
    n, m = len(read), frame_len
    if n == 0:
        return [None] * m

    # cost[i][j] = cheapest alignment of read[:i] to slots[:j]
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
        back[i][0] = "D"          # delete from read
    for j in range(1, m + 1):
        cost[0][j] = j
        back[0][j] = "I"          # slot left empty

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            # Slots are positional placeholders, so a substitution cost cannot
            # depend on the slot's "expected" character. Use a flat sub cost and
            # let the confusion bias apply only when the read char is ambiguous
            # at all, which keeps identical-length reads a strict diagonal.
            sub = cost[i - 1][j - 1] + (0.25 if read[i - 1] in _CONFUSABLE else 1.0)
            dele = cost[i - 1][j] + 1.0
            ins = cost[i][j - 1] + 1.0
            best = min(sub, dele, ins)
            cost[i][j] = best
            back[i][j] = "S" if best == sub else ("D" if best == dele else "I")

    slots: list[str | None] = [None] * m
    i, j = n, m
    while i > 0 and j > 0:
        op = back[i][j]
        if op == "S":
            slots[j - 1] = read[i - 1]
            i, j = i - 1, j - 1
        elif op == "D":
            i -= 1
        else:
            j -= 1
    return slots


def _beam_candidates(
    tallies: list[Counter],
    beam_width: int = _SNAP_BEAM_WIDTH,
    top_k: int = _SNAP_TOP_K,
) -> list[tuple[str, float]]:
    """Highest-scoring character strings from the per-position tallies.

    Score is the sum of ``log(vote share)`` across positions, so it is the
    argmax string that comes out on top and each alternative carries an explicit,
    comparable cost. Returned best-first.
    """
    beams: list[tuple[str, float]] = [("", 0.0)]
    for tally in tallies:
        if not tally:
            continue
        total = sum(tally.values()) or 1.0
        options = tally.most_common(top_k)
        nxt: list[tuple[str, float]] = []
        for prefix, score in beams:
            for ch, weight in options:
                share = max(weight / total, 1e-9)
                nxt.append((prefix + ch, score + math.log(share)))
        nxt.sort(key=lambda cs: -cs[1])
        beams = nxt[:beam_width]
    return beams


def _snap(tallies: list[Counter], argmax: str) -> tuple[str, bool, str, float]:
    """Resolve the vote into a plate string.

    ``matches_format`` rather than ``validate_plate`` is used to test candidates:
    the validator both checks *and* repairs, and letting its repairs back in here
    would undo the point of choosing the candidate ourselves.

    Returns ``(plate, format_valid, method, cost)``. ``cost`` is how much vote
    score was given up versus the argmax, in nats — 0.0 when the argmax was
    already legal. Exposed in the audit trail because "we changed one character
    to make this a valid plate" is something an operator should be able to see.
    """
    if matches_format(argmax):
        return argmax, True, "argmax", 0.0

    candidates = _beam_candidates(tallies)
    if candidates:
        best_score = candidates[0][1]
        # Every legal candidate inside the budget, not just the first one found.
        # Taking the first meant the vote alone decided between two legal plates,
        # and the vote cannot see that one of them is a shape the RTO issues and
        # the other is a shape that almost always means a dropped character.
        legal = [
            (cand, best_score - score)
            for cand, score in candidates
            if matches_format(cand) and (best_score - score) <= _MAX_SNAP_COST
        ]
        if legal:
            # Rank by vote cost, discounted by how typical the resulting plate
            # shape is. _TYPICALITY_WEIGHT is in the same nats as the beam cost,
            # so it can overturn a near-tie and cannot overturn real evidence:
            # at 0.8 it buys about the difference between a 4-digit tail (1.0)
            # and a 3-digit one (0.45), and no more.
            best = min(
                legal,
                key=lambda cs: cs[1] - _TYPICALITY_WEIGHT * math.log(
                    max(format_typicality(cs[0]), 1e-3)
                ),
            )
            return best[0], True, "beam_snap", round(best[1], 4)

    # Nothing legal within budget. Let the validator try its own character
    # fixes; if that fails too, return the raw consensus — an unsnapped string
    # is still a lead worth showing, it just doesn't get the format bonus.
    fixed = validate_plate(argmax)
    if fixed:
        return fixed, True, "validator_fuzzy", 0.0
    return argmax, False, "unsnapped", 0.0


def consensus_plate(
    reads: list[tuple[str | None, float | None]],
) -> tuple[str | None, float, int, dict]:
    """Fold every plate read from one track into a single consensus string.

    Args:
        reads: ``[(text, confidence), …]`` — one entry per frame that produced a
            plate. ``None`` text and ``None`` confidence are tolerated and
            skipped, so callers can pass a raw column straight from the DB.

    Returns:
        ``(plate, confidence, read_count, votes)`` where *votes* is a
        JSON-serialisable audit trail: the per-position tallies and the
        alternates that lost. It is stored in ``tracks.plate_votes`` and
        surfaced in the UI, because an operator acting on a plate needs to see
        whether it rested on nine agreeing reads or on one lucky guess.
    """
    # Reads the OCR itself declined to make are dropped before the ballot, not
    # scored down in it. The India-specific model emits the literal word
    # UNREADABLE on crops it cannot resolve; letting that vote meant a track
    # with four illegible frames and one good one could out-vote the good one
    # with a word.
    cleaned: list[tuple[str, float]] = []
    abstained = 0
    for text, conf in reads:
        t = _clean(text)
        if len(t) < _MIN_READ_LEN:
            continue
        if rto.is_abstention(t):
            abstained += 1
            continue
        cleaned.append((t, float(conf) if conf is not None else 0.5))

    if not cleaned:
        return None, 0.0, 0, {"reads": 0, "abstained": abstained,
                              "method": "no_usable_read"}

    if len(cleaned) == 1:
        only = cleaned[0]
        snapped = validate_plate(only[0])
        votes = {"reads": 1, "abstained": abstained, "method": "single_read",
                 "raw": [only[0]]}
        if snapped is None:
            # One read that is not a registration is nothing at all. It used to
            # be returned raw, which is how this estate acquired vehicles
            # plated KH2222 and UNRD71.
            votes["rejected_raw"] = only[0]
            return None, 0.0, 1, votes
        # A single read is a lead, never a conclusion — cap what it can claim.
        return snapped, round(min(only[1], 0.28), 4), 1, votes

    # ── Pick the alignment frame: confidence-weighted modal length ───────────
    len_weight: Counter = Counter()
    for text, conf in cleaned:
        len_weight[len(text)] += conf
    frame_len = max(len_weight.items(), key=lambda kv: (kv[1], kv[0]))[0]

    # ── Vote, position by position ──────────────────────────────────────────
    tallies: list[Counter] = [Counter() for _ in range(frame_len)]
    for text, conf in cleaned:
        if len(text) == frame_len:
            slots: list[str | None] = list(text)
            weight = conf
        else:
            slots = _align(text, frame_len)
            weight = conf * _MISALIGNED_WEIGHT
        for pos, ch in enumerate(slots):
            if ch is None:
                continue
            tallies[pos][ch] += weight
            for alt in _CONFUSABLE.get(ch, ()):  # glyph-class partial credit
                tallies[pos][alt] += weight * _CONFUSION_CREDIT

    chars: list[str] = []
    audit: list[dict] = []
    for pos, tally in enumerate(tallies):
        if not tally:
            audit.append({"pos": pos, "winner": None, "top": []})
            continue
        ranked = tally.most_common(4)
        chars.append(ranked[0][0])
        audit.append({
            "pos": pos,
            "winner": ranked[0][0],
            "top": [[c, round(w, 3)] for c, w in ranked],
        })

    raw_consensus = "".join(chars)

    # ── Snap to the Indian plate grammar ────────────────────────────────────
    plate, format_valid, snap_method, snap_cost = _snap(tallies, raw_consensus)

    # ── Confidence margins, measured on the characters we actually returned ──
    # Not on the argmax. If snapping traded a character for its runner-up, the
    # confidence must reflect the runner-up's weaker support — otherwise the
    # grammar bonus would silently pay for the swap twice over.
    voted = [t for t in tallies if t]
    margins: list[float] = []
    if len(plate) == len(voted):
        for ch, tally in zip(plate, voted):
            total = sum(tally.values())
            margins.append(tally.get(ch, 0.0) / total if total > 0 else 0.0)
    else:
        # Validator fuzz changed the length; fall back to argmax margins.
        for tally in voted:
            total = sum(tally.values())
            margins.append(tally.most_common(1)[0][1] / total if total > 0 else 0.0)

    # ── Confidence ──────────────────────────────────────────────────────────
    # Per-position vote margin (how decisive each character was) × a saturating
    # function of the read count (how much evidence there was at all) × a bonus
    # for landing on a grammatically valid plate.
    mean_margin = sum(margins) / len(margins) if margins else 0.0
    n = len(cleaned)
    evidence = 1.0 - math.exp(-n / 3.0)      # 1→0.28, 3→0.63, 6→0.86, 10→0.96
    confidence = mean_margin * evidence * _METHOD_TRUST.get(snap_method, 0.75)

    votes = {
        "reads": n,
        "abstained": abstained,
        "method": "multiframe_vote",
        "frame_len": frame_len,
        "raw_consensus": raw_consensus,
        "format_valid": format_valid,
        "snap_method": snap_method,
        "snap_cost": snap_cost,
        "mean_margin": round(mean_margin, 4),
        "evidence": round(evidence, 4),
        "positions": audit,
        "raw": [t for t, _ in cleaned][:20],
        "rto_known": rto.is_known_rto(plate) if format_valid else None,
    }

    # The vote produced a string; the grammar says it is not a registration.
    # Returning it anyway is what put UNREADBLE, TA0AAKS10 and M062026 in front
    # of operators as vehicle identities. The ballot is kept in the audit trail
    # so the track still shows what was seen — but the *plate* column, which is
    # what an officer acts on, stays empty, because empty is the truth.
    if not format_valid:
        votes["rejected_raw"] = plate
        return None, 0.0, n, votes

    return plate, round(min(confidence, 0.99), 4), n, votes


def plate_similarity(a: str | None, b: str | None) -> float:
    """Confusion-aware similarity between two plate strings, 0–1.

    A substitution inside a glyph confusion class costs a quarter of a normal
    substitution, so ``GJ01AB1234`` and ``GJ0IAB1Z34`` score ~0.95 rather than
    the 0.8 a plain edit distance would give. Dragnet uses this for fuzzy plate
    matching, where treating O/0 as a mismatch would throw away most of the
    true hits.
    """
    a, b = _clean(a), _clean(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    n, m = len(a), len(b)
    prev = [float(j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [float(i)] + [0.0] * m
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                sub_cost = 0.0
            elif b[j - 1] in _CONFUSABLE.get(a[i - 1], set()):
                sub_cost = 0.25
            else:
                sub_cost = 1.0
            cur[j] = min(prev[j - 1] + sub_cost, prev[j] + 1.0, cur[j - 1] + 1.0)
        prev = cur
    distance = prev[m]
    return max(0.0, 1.0 - distance / max(n, m))
