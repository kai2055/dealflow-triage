"""Tests for the cost guard: prices from OpenRouter's list, the estimate, and only "yes" counts."""

import pytest

from triage.cost import (Price, PriceUnavailable, confirmed, estimate, is_openrouter, parse_prices,
                         price_for)

LISTING = {"data": [
    {"id": "anthropic/claude-sonnet-5.5", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
    {"id": "openai/gpt-6-luna", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
    {"id": "broken/model", "pricing": {}},
]}


def test_prices_are_read_per_million_tokens():
    prices = parse_prices(LISTING)
    assert prices["anthropic/claude-sonnet-5.5"] == Price(pytest.approx(2.0), pytest.approx(10.0))
    assert "broken/model" not in prices  # no usable price: treated as not listed


def test_unknown_model_is_refused_before_anything_is_sent():
    with pytest.raises(PriceUnavailable, match="check the spelling"):
        price_for("anthropic/claude-sonet-5.5", parse_prices(LISTING))  # typo


def test_estimate_for_a_ten_page_deck():
    guess = estimate(10, Price(2.0, 10.0), max_retries=2)
    # input (10 x 1,900 + 2,500) x $2/M = $0.043; output 10,000 x $10/M = $0.10
    assert guess.expected == pytest.approx(0.143)
    assert guess.worst_case == pytest.approx(0.429)  # first try + 2 retries, all billed


def test_only_the_word_yes_counts():
    assert confirmed("?", ask=lambda _: " YES ")
    assert not confirmed("?", ask=lambda _: "y")
    assert not confirmed("?", ask=lambda _: "")


def test_openrouter_is_recognised_by_address():
    assert is_openrouter("https://openrouter.ai/api/v1")
    assert not is_openrouter("http://localhost:11434/v1")