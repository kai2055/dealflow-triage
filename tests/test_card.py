"""Tests for the pitch card: facts need evidence, formats are enforced, completeness is counted."""

import pytest
from pydantic import ValidationError

from triage.card import ClaimKind, Fact, PitchCard, Stage

CYCLICA = {
    "company_name": {"value": "Cyclica", "evidence": {"quote": "CYCLICA", "page": 1}},
    "stage": {"value": "series_b_or_later",
              "evidence": {"quote": "CAD$20M in a Series B round", "page": 9}},
    "round_size": {"value": {"amount": 20000000, "currency": "CAD", "status": "sought"},
                   "evidence": {"quote": "Seeking: CAD$20M in a Series B round", "page": 9}},
    "headquarters": {"value": "Toronto, Ontario",
                     "evidence": {"quote": "Toronto, Ontario M5J 1A7", "page": 10}},
    "traction": [{"kind": "partnership", "text": "Works with Merck KGaA",
                  "evidence": {"quote": "Merck KGaA Darmstadt, Germany", "page": 7}}],
}


def test_empty_card_is_valid_and_zero_complete():
    card = PitchCard()
    assert card.completeness() == 0.0
    assert card.headquarters.value is None


def test_real_deck_example_is_accepted():
    card = PitchCard.model_validate(CYCLICA)
    assert card.stage.value == Stage.SERIES_B_OR_LATER
    assert card.round_size.value.currency == "CAD"
    assert card.headquarters.value == "Toronto, Ontario"
    assert card.completeness() == round(5 / 11, 2)


def test_value_without_quote_is_refused():
    with pytest.raises(ValidationError):
        Fact[str](value="Canada")


def test_quote_without_value_is_refused():
    with pytest.raises(ValidationError):
        Fact[str](evidence={"quote": "Toronto", "page": 10})


def test_currency_must_be_a_three_letter_code():
    bad = {**CYCLICA, "round_size": {"value": {"amount": 20, "currency": "dollars", "status": "sought"},
                                     "evidence": {"quote": "20 dollars", "page": 9}}}
    with pytest.raises(ValidationError):
        PitchCard.model_validate(bad)

def test_currency_can_be_empty_when_only_a_dollar_sign_is_written():
    gridcure = {"round_size": {"value": {"amount": 95000, "currency": None, "status": "raised"},
                               "evidence": {"quote": "$95K seed", "page": 8}}}
    assert PitchCard.model_validate(gridcure).round_size.value.currency is None


def test_stage_outside_the_list_is_refused():
    with pytest.raises(ValidationError):
        PitchCard.model_validate({"stage": {"value": "unicorn", "evidence": {"quote": "x", "page": 1}}})


def test_potential_partnership_is_its_own_kind():
    card = PitchCard.model_validate({"traction": [{
        "kind": "potential_partnership", "text": "Partnering opportunities with Aramex",
        "evidence": {"quote": "Partnering Opportunities", "page": 9}}]})
    assert card.traction[0].kind == ClaimKind.POTENTIAL_PARTNERSHIP


def test_page_numbers_start_at_one():
    with pytest.raises(ValidationError):
        Fact[str](value="Cyclica", evidence={"quote": "CYCLICA", "page": 0})


def test_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        PitchCard.model_validate({"hq_country": {"value": "Canada", "evidence": {"quote": "x", "page": 1}}})