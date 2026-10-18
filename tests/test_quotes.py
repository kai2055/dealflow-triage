"""Tests for the quote check: layout is forgiven, content never is, and every quote on the card is checked."""

from pathlib import Path

from triage.card import Claim, ClaimKind, Evidence, Fact, Person, PitchCard
from triage.pages import Page
from triage.quotes import QuoteStatus, check_quotes, tidy

NO_PICTURE = Path("unused.jpg")  # the quote check only reads text


def card_with_one_fact(quote, page):
    return PitchCard(company_name=Fact[str](value="Decisive Health", evidence=Evidence(quote=quote, page=page)))


def only_check(card, pages):
    [check] = check_quotes(card, pages).checks
    return check


def test_honest_quote_with_line_breaks_and_capitals_is_verified():
    pages = [Page(1, "CLEAR MEDICAL \nBILLING \nOCTOBER,\n2015\nDECISIVE HEALTH ", NO_PICTURE)]
    check = only_check(card_with_one_fact("Clear Medical Billing", 1), pages)
    assert check.status == QuoteStatus.VERIFIED


def test_changed_number_is_a_bad_quote():
    pages = [Page(1, "We have signed $1.2M in contracts", NO_PICTURE)]
    check = only_check(card_with_one_fact("signed $12M in contracts", 1), pages)
    assert check.status == QuoteStatus.BAD_QUOTE  # one dot apart, still caught


def test_quote_on_the_wrong_page_is_a_bad_quote():
    pages = [Page(1, "Team", NO_PICTURE), Page(2, "Raising CAD 20M", NO_PICTURE)]
    check = only_check(card_with_one_fact("Raising CAD 20M", 1), pages)
    assert check.status == QuoteStatus.BAD_QUOTE
    assert check.reason == "not on page 1"


def test_page_that_does_not_exist_is_a_bad_quote():
    pages = [Page(1, "Only one page", NO_PICTURE)]
    check = only_check(card_with_one_fact("Only one page", 14), pages)
    assert check.status == QuoteStatus.BAD_QUOTE
    assert "no page 14" in check.reason


def test_quote_on_an_image_only_page_is_unverifiable():
    pages = [Page(1, "", NO_PICTURE)]
    check = only_check(card_with_one_fact("Cyclica", 1), pages)
    assert check.status == QuoteStatus.UNVERIFIABLE


def test_look_alike_characters_are_unified():
    pages = [Page(1, "\u201cAI-\ufb01rst\u201d platform \u2013 2014\u20132016", NO_PICTURE)]
    check = only_check(card_with_one_fact('"AI-first" platform - 2014-2016', 1), pages)
    assert check.status == QuoteStatus.VERIFIED


def test_line_end_hyphens_are_read_both_ways():
    pages = [Page(1, "We streamline opera-\ntions with a state-of-\nthe-art engine", NO_PICTURE)]
    assert only_check(card_with_one_fact("streamline operations", 1), pages).status == QuoteStatus.VERIFIED
    assert only_check(card_with_one_fact("state-of-the-art engine", 1), pages).status == QuoteStatus.VERIFIED
    dash_list = [Page(1, "Revenue -\n$1M", NO_PICTURE)]
    assert only_check(card_with_one_fact("Revenue -$1M", 1), dash_list).status == QuoteStatus.BAD_QUOTE


def test_every_quote_on_the_card_is_checked_and_counted():
    pages = [Page(1, "GridCure Seed round", NO_PICTURE), Page(2, "Jane Doe, CEO", NO_PICTURE),
             Page(3, "", NO_PICTURE)]
    card = PitchCard(
        company_name=Fact[str](value="GridCure", evidence=Evidence(quote="GridCure", page=1)),
        team=[Person(name="Jane Doe", role="CEO", evidence=Evidence(quote="Jane Doe, CEO", page=2))],
        traction=[Claim(kind=ClaimKind.CONTRACTS, text="$12M contracts",
                        evidence=Evidence(quote="$12M in contracts", page=1)),
                  Claim(kind=ClaimKind.CUSTOMERS, text="3 utilities",
                        evidence=Evidence(quote="3 utilities", page=3))],
    )
    report = check_quotes(card, pages)
    assert [c.field for c in report.checks] == ["company_name", "team[0]", "traction[0]", "traction[1]"]
    assert report.count(QuoteStatus.VERIFIED) == 2
    assert report.count(QuoteStatus.BAD_QUOTE) == 1
    assert report.count(QuoteStatus.UNVERIFIABLE) == 1


def test_empty_card_has_nothing_to_check():
    assert check_quotes(PitchCard(), [Page(1, "Anything", NO_PICTURE)]).checks == ()


def test_tidy_never_removes_letters_digits_or_punctuation():
    assert tidy("  $1.2M,\n\nSeed  ") == "$1.2m, seed"