"""The cost guard: today's price from OpenRouter, an estimate before a run, and nothing sent without "yes".

Decisions 36, 75, 76: never agree to a stale or unknown amount.
"""

import json
import urllib.request
from dataclasses import dataclass

PRICE_LIST_URL = "https://openrouter.ai/api/v1/models"  # public, no key, free
TOKENS_PER_PAGE = 1900  # one 1568px page picture plus its text
FIXED_INPUT_TOKENS = 2500  # instructions and card layout
MAX_OUTPUT_TOKENS = 10000  # thinking plus the card, on the high side


class PriceUnavailable(Exception):
    """No current price for this model, so nothing may be sent."""


@dataclass(frozen=True)
class Price:
    input_per_million: float  # dollars
    output_per_million: float


@dataclass(frozen=True)
class Estimate:
    expected: float  # dollars, one call
    worst_case: float  # dollars, every retry used and billed (decision 54)


def fetch_prices(url: str = PRICE_LIST_URL) -> dict[str, Price]:
    """Today's prices for every model on OpenRouter."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            listing = json.load(response)
    except Exception as e:
        raise PriceUnavailable(f"could not read OpenRouter's price list: {e}") from e
    return parse_prices(listing)


def parse_prices(listing: dict) -> dict[str, Price]:
    prices = {}
    for model in listing.get("data", []):
        pricing = model.get("pricing", {})
        try:  # OpenRouter gives dollars per token, as text
            prices[model["id"]] = Price(float(pricing["prompt"]) * 1e6, float(pricing["completion"]) * 1e6)
        except (KeyError, TypeError, ValueError):
            continue  # a model without a usable price is treated as not listed
    return prices


def price_for(model: str, prices: dict[str, Price]) -> Price:
    if model not in prices:
        raise PriceUnavailable(f"{model} is not in OpenRouter's price list (check the spelling)")
    return prices[model]


def estimate(page_count: int, price: Price, max_retries: int) -> Estimate:
    input_tokens = page_count * TOKENS_PER_PAGE + FIXED_INPUT_TOKENS
    one_call = (input_tokens * price.input_per_million + MAX_OUTPUT_TOKENS * price.output_per_million) / 1e6
    return Estimate(one_call, one_call * (1 + max_retries))


def is_openrouter(base_url: str) -> bool:
    return "openrouter.ai" in base_url


def confirmed(question: str, ask=input) -> bool:
    """Only the exact word "yes" counts as agreement."""
    return ask(question).strip().lower() == "yes"