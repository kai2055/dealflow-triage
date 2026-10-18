"""Deck to pages: each PDF page as its text plus a picture, kept on disk by the deck's fingerprint."""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from triage.storage import RawStore

DEFAULT_PAGES_DIR = Path("data/pages")
DEFAULT_LONG_EDGE = 1568  # pixels on the longest side of each page picture
DEFAULT_MAX_PAGES = 60


class DeckError(Exception):
    """A deck that can't be turned into pages. `reason` is one of REASONS."""

    REASONS = ("locked", "unreadable", "no_pages", "too_many_pages")

    def __init__(self, reason: str, detail: str):
        if reason not in self.REASONS:
            raise ValueError(f"Unknown reason: {reason!r}")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Page:
    number: int  # starts at 1, the same number a card's evidence uses
    text: str  # empty for image-only pages
    image_path: Path

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True)
class DeckPages:
    deck_sha256: str
    long_edge: int
    pages: tuple[Page, ...]


def _page_files(folder: Path, number: int) -> tuple[Path, Path]:
    return folder / f"page-{number:03d}.png", folder / f"page-{number:03d}.txt"


def _load(folder: Path, deck_sha256: str, long_edge: int) -> DeckPages | None:
    """Return the cached pages if the folder is complete and matches, else None."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.get("deck_sha256") != deck_sha256 or manifest.get("long_edge") != long_edge:
        return None
    pages = []
    for number in range(1, manifest.get("page_count", 0) + 1):
        png, txt = _page_files(folder, number)
        if not (png.exists() and txt.exists()):
            return None
        pages.append(Page(number, txt.read_text(encoding="utf-8"), png))
    return DeckPages(deck_sha256, long_edge, tuple(pages)) if pages else None


def _render(pdf_bytes: bytes, folder: Path, long_edge: int, max_pages: int) -> int:
    """Write every page's picture and text into `folder`. Returns the page count."""
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as e:
        # PDFium refuses zero-page PDFs without setting an error code, so err_code may be
        # left over from an earlier file. Known limit: an empty PDF read right after a
        # locked one is labelled "locked". Every other empty PDF comes out "unreadable".
        if e.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise DeckError("locked", "the PDF needs a password to open") from e
        raise DeckError("unreadable", "could not open: damaged or has no pages") from e
    try:
        count = len(pdf)
        if count == 0:  # safeguard in case a future PDFium opens empty PDFs
            raise DeckError("no_pages", "the PDF opens but has no pages")
        if count > max_pages:
            raise DeckError("too_many_pages", f"{count} pages, limit is {max_pages}")
        for index in range(count):
            page = pdf[index]
            try:
                width, height = page.get_size()
                scale = long_edge / max(width, height)
                png, txt = _page_files(folder, index + 1)
                page.render(scale=scale).to_pil().save(png, format="PNG")
                text = page.get_textpage().get_text_range()
                txt.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
            except DeckError:
                raise
            except Exception as e:  # one bad page fails the whole deck (decision 48)
                raise DeckError("unreadable", f"page {index + 1}: {e}") from e
            finally:
                page.close()
        return count
    finally:
        pdf.close()


def deck_to_pages(
    store: RawStore,
    deck_sha256: str,
    root: Path = DEFAULT_PAGES_DIR,
    long_edge: int = DEFAULT_LONG_EDGE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> DeckPages:
    """Turn a stored deck into pages, rendering it once and reusing the result after that."""
    folder = Path(root) / deck_sha256 / f"{long_edge}px"
    cached = _load(folder, deck_sha256, long_edge)
    if cached:
        return cached

    pdf_bytes = store.load(deck_sha256)  # also checks the fingerprint
    folder.parent.mkdir(parents=True, exist_ok=True)
    if folder.exists():  # left incomplete by hand; the raw file can always rebuild it
        shutil.rmtree(folder)

    tmp = Path(tempfile.mkdtemp(dir=folder.parent, prefix=".tmp-"))
    try:
        count = _render(pdf_bytes, tmp, long_edge, max_pages)
        manifest = {
            "deck_sha256": deck_sha256,
            "long_edge": long_edge,
            "page_count": count,
            "renderer": f"pypdfium2 {pdfium.version.PYPDFIUM_INFO}",
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, folder)  # the folder appears whole, or not at all
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return _load(folder, deck_sha256, long_edge)