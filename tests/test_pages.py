"""Tests for deck to pages: page numbers, text, pictures, the cache, and every named failure."""

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image

from triage import pages as pages_module
from triage.pages import DeckError, deck_to_pages
from triage.storage import RawStore


def make_pdf(page_texts, width=960, height=540):
    """Build a small real PDF by hand. None makes a page with no text (like an image-only slide)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in page_texts:
        stream = f"BT /F1 24 Tf 72 300 Td ({text}) Tj ET" if text else ""
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
        assert img.format == "PNG"
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