"""Tests for deck to pages: page numbers, text, pictures, the cache, and every named failure."""

import json

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image

from triage import pages as pages_module
from triage.card import Evidence, Fact, PitchCard
from triage.pages import DeckError, clean_text, deck_to_pages
from triage.quotes import QuoteStatus, check_quotes
from triage.storage import RawStore


def make_pdf(page_texts, width=960, height=540):
    """Build a small real PDF by hand. None makes a page with no text (like an image-only slide);
    a list of strings makes one line per string."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in page_texts:
        lines = text if isinstance(text, list) else [text]
        stream = ("BT /F1 24 Tf 72 300 Td " + " 0 -30 Td ".join(f"({line}) Tj" for line in lines) + " ET"
                  if text else "")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_ref = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_ref} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"

    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


@pytest.fixture
def store(tmp_path):
    return RawStore(tmp_path / "raw")


@pytest.fixture
def pages_dir(tmp_path):
    return tmp_path / "pages"


def test_pages_are_numbered_from_one_with_text_and_picture(store, pages_dir):
    sha = store.save(make_pdf(["Seed round 95K", None]))
    deck = deck_to_pages(store, sha, pages_dir)
    assert [p.number for p in deck.pages] == [1, 2]
    assert "Seed round 95K" in deck.pages[0].text
    assert deck.pages[0].has_text
    assert not deck.pages[1].has_text  # image-only page: no text, but still a page
    assert all(p.image_path.exists() for p in deck.pages)


def test_picture_long_edge_matches_the_setting(store, pages_dir):
    sha = store.save(make_pdf(["Wide slide"], width=960, height=540))
    deck = deck_to_pages(store, sha, pages_dir, long_edge=1568)
    with Image.open(deck.pages[0].image_path) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1568


def test_second_call_reuses_the_saved_pages(store, pages_dir, monkeypatch):
    sha = store.save(make_pdf(["Rendered once"]))
    first = deck_to_pages(store, sha, pages_dir)

    def must_not_render(*args, **kwargs):
        raise AssertionError("rendered a deck that was already saved")

    monkeypatch.setattr(pages_module, "_render", must_not_render)
    assert deck_to_pages(store, sha, pages_dir) == first


def test_each_size_gets_its_own_folder(store, pages_dir):
    sha = store.save(make_pdf(["Two sizes"]))
    big = deck_to_pages(store, sha, pages_dir, long_edge=1568)
    small = deck_to_pages(store, sha, pages_dir, long_edge=768)
    assert big.pages[0].image_path.parent.name == "1568px"
    assert small.pages[0].image_path.parent.name == "768px"
    assert big.pages[0].image_path.exists() and small.pages[0].image_path.exists()


def test_incomplete_folder_is_rebuilt(store, pages_dir):
    sha = store.save(make_pdf(["Page one", "Page two"]))
    deck = deck_to_pages(store, sha, pages_dir)
    deck.pages[1].image_path.unlink()
    rebuilt = deck_to_pages(store, sha, pages_dir)
    assert rebuilt.pages[1].image_path.exists()


def test_locked_pdf_is_named_locked(store, pages_dir, monkeypatch):
    sha = store.save(make_pdf(["Secret"]))

    def needs_password(*args, **kwargs):
        raise pdfium.PdfiumError("Incorrect password error", err_code=pdfium_c.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(pdfium, "PdfDocument", needs_password)
    with pytest.raises(DeckError) as caught:
        deck_to_pages(store, sha, pages_dir)
    assert caught.value.reason == "locked"


def test_broken_pdf_is_named_unreadable_and_leaves_nothing_behind(store, pages_dir):
    sha = store.save(make_pdf(["Cut off"])[:60])
    with pytest.raises(DeckError) as caught:
        deck_to_pages(store, sha, pages_dir)
    assert caught.value.reason == "unreadable"
    assert list((pages_dir / sha).iterdir()) == []  # no half-written folder


def test_pdf_without_pages_is_refused_as_unreadable(store, pages_dir):
    sha = store.save(make_pdf([]))
    with pytest.raises(DeckError) as caught:
        deck_to_pages(store, sha, pages_dir)
    assert caught.value.reason == "unreadable"  # PDFium won't open empty PDFs (decision 48, amended)
    assert list((pages_dir / sha).iterdir()) == []


def test_deck_over_the_page_limit_is_refused(store, pages_dir):
    sha = store.save(make_pdf(["One", "Two", "Three"]))
    with pytest.raises(DeckError) as caught:
        deck_to_pages(store, sha, pages_dir, max_pages=2)
    assert caught.value.reason == "too_many_pages"
    assert list((pages_dir / sha).iterdir()) == []





def test_changed_quality_renders_again(store, pages_dir):
    sha = store.save(make_pdf(["Quality"]))
    deck_to_pages(store, sha, pages_dir, jpeg_quality=90)
    redone = deck_to_pages(store, sha, pages_dir, jpeg_quality=60)
    assert redone.jpeg_quality == 60
    assert redone.pages[0].image_path.suffix == ".jpg"


# Decisions 81 and 83: junk words dropped, the split-word mark read as printed, raw text kept.

GRIDCURE_PAGE_2_LINE = ('\x1a!% \x18\x19"@\x04\x1b"\x1d\x18\x17%"\x19\x17!\x1f\x01\x01\x01\x01\x01\x01\x16 '
                        '\x1b\x19\x1e\x17! \x1b"\x1d\x18\x17%"\x19\x01\x01\x01 CURRENT PAIN')


def test_clean_text_keeps_the_real_words_from_a_gridcure_junk_line():
    text, dropped = clean_text(GRIDCURE_PAGE_2_LINE + "\r\n\x0c.\x0c\r\n$12M\tCONTRACTS")
    assert text == "CURRENT PAIN\n$12M\tCONTRACTS"  # a clean line keeps its tab exactly
    assert dropped == 5


def test_junk_words_are_dropped_and_the_raw_text_is_kept(store, pages_dir):
    sha = store.save(make_pdf(["\\001\\004junk CURRENT PAIN", "Clean slide"]))
    deck = deck_to_pages(store, sha, pages_dir)
    junky, clean = deck.pages
    assert junky.text == "CURRENT PAIN" and not junky.text_complete
    assert clean.text == "Clean slide" and clean.text_complete
    raw = (junky.image_path.parent / "page-001.raw.txt").read_text(encoding="utf-8")
    assert "\x01" in raw  # nothing lost: the untouched text sits beside the cleaned one


def test_page_of_only_junk_counts_as_picture_only(store, pages_dir):
    sha = store.save(make_pdf(["\\001\\004"]))
    page = deck_to_pages(store, sha, pages_dir).pages[0]
    assert not page.has_text and not page.text_complete


def test_split_word_reads_as_printed_and_its_quote_is_verified(store, pages_dir):
    sha = store.save(make_pdf([["We streamline opera-", "tions daily"]]))
    pages = deck_to_pages(store, sha, pages_dir).pages
    assert "opera-\ntions" in pages[0].text  # PDFium's mark turned back into hyphen + line break
    card = PitchCard(one_liner=Fact[str](value="x", evidence=Evidence(quote="streamline operations", page=1)))
    [check] = check_quotes(card, pages).checks
    assert check.status == QuoteStatus.VERIFIED


def test_pages_saved_before_the_cleaning_rules_are_rebuilt(store, pages_dir):
    sha = store.save(make_pdf(["\\001\\004junk CURRENT PAIN"]))
    deck = deck_to_pages(store, sha, pages_dir)
    manifest_path = deck.pages[0].image_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["text_cleanup"]  # what a folder saved by the old pages.py looks like
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert deck_to_pages(store, sha, pages_dir).pages[0].text == "CURRENT PAIN"
    assert "text_cleanup" in json.loads(manifest_path.read_text(encoding="utf-8"))