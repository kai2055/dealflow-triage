"""Quote check: is each quote on a card really on the page it names? Code checks, no model involved."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from triage.card import Evidence, Fact, PitchCard
from triage.pages import Page


class QuoteStatus(str, Enum):
    VERIFIED = "verified"  # found on the page, word for word after tidying (decision 59)
    NOT_IN_TEXT = "not_in_text"  # quote words missing from the page's text: a person should look (decision 84)
    UNVERIFIABLE = "unverifiable"  # no text layer, or the words are there in another order (decisions 60, 82)
    BAD_QUOTE = "bad_quote"  # proven wrong: the page does not exist (decision 84)


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


def _bare(word: str) -> str:
    """A word without punctuation or symbols stuck to its start or end: '$12m,' -> '12m'."""
    start, end = 0, len(word)
    while start < end and unicodedata.category(word[start])[0] in "PS":
        start += 1
    while end > start and unicodedata.category(word[end - 1])[0] in "PS":
        end -= 1
    return word[start:end]


def _words(tidied: str) -> Counter:
    """How often each bare word appears; words that were only punctuation are left out."""
    return Counter(w for w in map(_bare, tidied.split(" ")) if w)


def _words_all_present(quote: str, versions: set[str]) -> bool:
    """Decision 82: every quote word is on the page as a whole word, at least as often as in
    the quote, anywhere on the page. Order is ignored because the text layer's order can't be
    trusted (finding 13); this can only make a quote unverifiable, never verified."""
    needed = _words(quote)
    return any(not (needed - _words(version)) for version in versions)


def _check(field: str, evidence: Evidence, pages: dict[int, Page]) -> QuoteCheck:
    page = pages.get(evidence.page)
    if page is None:
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.BAD_QUOTE,
                          f"no page {evidence.page}; the deck has {len(pages)} pages")
    if not page.has_text:
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.UNVERIFIABLE,
                          f"page {evidence.page} has no text layer")
    quote = tidy(evidence.quote)
    versions = _page_versions(page.text)
    if any(quote in version for version in versions):
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.VERIFIED,
                          f"found on page {evidence.page}")
    if _words_all_present(quote, versions):
        return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.UNVERIFIABLE,
                          f"words on page {evidence.page}, order differs")
        # Words inside pictures never reach the text layer (finding 15), so a missing word is not proof.
    incomplete = "" if page.text_complete else ", which is incomplete (junk removed)"
    return QuoteCheck(field, evidence.quote, evidence.page, QuoteStatus.NOT_IN_TEXT,
                      f"not in the text of page {evidence.page}{incomplete}")


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