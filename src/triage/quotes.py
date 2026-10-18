"""Quote check: is each quote on a card really on the page it names? Code checks, no model involved."""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from triage.card import Evidence, Fact, PitchCard
from triage.pages import Page


class QuoteStatus(str, Enum):
    VERIFIED = "verified"
    BAD_QUOTE = "bad_quote"
    UNVERIFIABLE = "unverifiable"  # the page has no text layer (decision 60)


@dataclass(frozen=True)
class QuoteCheck:
    field: str  # where the quote sits on the card, e.g. "round_size", "team[0]", "traction[2]"
    quote: str
    page: int
    status: QuoteStatus
    reason: str


@dataclass(frozen=True)
class QuoteReport:
    """Every quote on one card with its result. The card itself is never changed (decision 61)."""

    checks: tuple[QuoteCheck, ...]

    def count(self, status: QuoteStatus) -> int:
        return sum(c.status == status for c in self.checks)


# Characters that look alike but are different codes; unified on both sides before comparing.
_LOOKALIKES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',  # curly quotes
    "\u2013": "-", "\u2014": "-", "\u2212": "-",  # en dash, em dash, minus sign
})
_LINE_END_HYPHEN = re.compile(r"(?<=\w)-[ \t]*\n\s*(?=\w)")  # only between two letters or digits


def tidy(text: str) -> str:
    """Remove layout differences only: never letters, digits or punctuation (decision 59)."""
    text = unicodedata.normalize("NFKC", text)  # the "fi" ligature -> "fi", non-breaking space -> space
    text = text.translate(_LOOKALIKES)
    text = re.sub(r"\s+", " ", text)  # line breaks and runs of spaces -> one space
    return text.casefold().strip()  # capitals ignored


def _page_versions(text: str) -> set[str]:
    """A hyphen at a line end is either a split word ("opera-\\ntions") or a real hyphen
    ("state-of-\\nthe-art"). The text can't tell us which, so both readings are kept."""
    return {tidy(_LINE_END_HYPHEN.sub("", text)), tidy(_LINE_END_HYPHEN.sub("-", text))}


def _check(field: str, evidence: Evidence, pages: dict[int, Page]) -> QuoteCheck:
    page = pages.get(evidence.page)
    if page is None:
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.BAD_QUOTE,
                          f"no page {evidence.page}; the deck has {len(pages)} pages")
    if not page.has_text:
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.UNVERIFIABLE,
                          f"page {evidence.page} has no text layer")
    quote = tidy(evidence.quote)
    if any(quote in version for version in _page_versions(page.text)):
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.VERIFIED,
                          f"found on page {evidence.page}")
    return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.BAD_QUOTE,
                      f"not on page {evidence.page}")


def check_quotes(card: PitchCard, pages: Sequence[Page]) -> QuoteReport:
    """Check every quote on the card: the single facts, each team member and each traction claim."""
    by_number = {p.number: p for p in pages}
    checks = []
    for name in PitchCard.model_fields:
        value = getattr(card, name)
        if isinstance(value, Fact) and value.evidence is not None:
            checks.append(_check(name, value.evidence, by_number))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                checks.append(_check(f"{name}[{i}]", item.evidence, by_number))
    return QuoteReport(tuple(checks))