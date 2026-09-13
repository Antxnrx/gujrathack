"""The registration grammar: what the template may decide, and what it may not.

Every case here came off a real crop in this estate. The template is a strong
prior and a strong prior is dangerous — most of these tests exist to pin the
line between *recovering* a character the camera could not resolve and
*inventing* one the prior would prefer.
"""

import pytest

from ml import rto
from ml.analytics.anpr import consensus_plate
from ml.plate_validator import validate_plate


# ── The template recovers what position makes certain ────────────────────────
def test_letter_position_resolves_a_zero_to_the_letter_o():
    """cam09, 100x25px. The sixth character of a ten-character plate is a
    letter by construction, so a round glyph there is O and not 0."""
    assert validate_plate("GJ11C05760") == "GJ11CO5760"


def test_digit_position_resolves_a_letter_to_a_digit():
    assert validate_plate("GJ11CO576O") == "GJ11CO5760"


def test_state_code_is_repaired_onto_a_real_state():
    assert validate_plate("GI01AB1234") == "GJ01AB1234"


def test_canonical_template_beats_a_cheaper_but_odd_parse():
    """``GJ1IC05532`` parses in one rewrite as GJ-1, series ICO — and in two as
    the canonical GJ-11, series CO. The canonical shape is right, so edit count
    must not dominate the typicality prior absolutely."""
    assert validate_plate("GJ1IC05532") == "GJ11CO5532"


# ── …and may not invent what it merely prefers ───────────────────────────────
def test_prior_may_not_buy_a_character_it_did_not_see():
    """A three-digit tail is unusual, and that is not licence to rewrite a
    legibly-read B into an 8 to manufacture the four-digit tail it expects."""
    assert validate_plate("GJ11AB457") == "GJ11AB457"


def test_an_unallocated_rto_is_flagged_not_rewritten():
    """GJ-28 has never been issued. The read is still what was read: rewriting
    it produced ``GJ2BAB1234``, inventing a three-letter series to escape a
    district the validator disbelieved."""
    assert validate_plate("GJ28AB1234") == "GJ28AB1234"
    assert rto.is_known_rto("GJ28AB1234") is False
    assert rto.is_known_rto("GJ11CO5760") is True


def test_other_states_are_never_judged_on_gujarats_list():
    assert rto.is_known_rto("MH12XY9876") is True
    assert rto.rto_plausibility("MH", "12") == pytest.approx(0.75)


# ── The OCR's own refusal ────────────────────────────────────────────────────
@pytest.mark.parametrize("word", [
    "UNREADABLE", "UNREADBLE", "UNRADBLE", "UNREADBL3", "UNREA9BLE",
    "UNUNREADBE", "UNREADBE", "UNREABLE",
])
def test_the_models_refusal_is_never_a_plate(word):
    """These twenty strings were stored as vehicle registrations across ten
    cameras. They are the OCR saying it could not read the crop."""
    assert rto.is_abstention(word) is True
    assert validate_plate(word) is None


def test_a_real_plate_is_not_mistaken_for_a_refusal():
    for plate in ("GJ11CO5760", "GJ18Z9883", "MH12XY9876", "GJ01AB1234"):
        assert rto.is_abstention(plate) is False


def test_abstentions_do_not_get_a_vote():
    """Four illegible frames must not out-vote one good read."""
    reads = [("UNREADBLE", 0.3)] * 4 + [("GJ01AB1234", 0.8)]
    plate, _conf, n, votes = consensus_plate(reads)
    assert plate == "GJ01AB1234"
    assert (n, votes["abstained"]) == (1, 4)


# ── An answer the grammar rejects is no answer ───────────────────────────────
def test_consensus_returns_no_plate_rather_than_a_non_plate():
    plate, conf, _n, votes = consensus_plate(
        [(r, 0.6) for r in ["XQ7WZZ", "QW9ZXK1", "ZX8QW7K", "WQ8XZ9K"]])
    assert plate is None, "a string the grammar rejects is not a registration"
    assert conf == 0.0
    assert votes["rejected_raw"]          # kept for audit, not for the operator


def test_single_unparseable_read_is_not_a_plate():
    plate, _conf, n, votes = consensus_plate([("KH2222", 0.9)])
    assert (plate, n) == (None, 1)
    assert votes["rejected_raw"] == "KH2222"


# ── Provenance the plate already carries ─────────────────────────────────────
def test_district_is_read_off_the_rto_code():
    assert rto.district("GJ11CO5760") == "Junagadh"
    assert rto.district("GJ18Z9883") == "Gandhinagar"
    assert rto.describe("GJ01AB1234") == "GJ 01 · Ahmedabad (West)"
    assert rto.district("MH12XY9876") is None      # not ours to name
    assert rto.district("GJ28AB1234") is None      # never allocated


def test_every_template_is_a_legal_registration_shape():
    """The shape list is generated, so this guards the generator rather than a
    hand-written list that silently dropped GJ 11 ABC 457."""
    assert len(rto.SHAPES) == 24
    assert rto.TEMPLATE in rto.SHAPES
    assert validate_plate("GJ11ABC457") == "GJ11ABC457"
