"""Deck to pages: each PDF page as its text plus a picture, kept on disk by the deck's fingerprint."""

import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from triage.storage import RawStore

DEFAULT_PAGES_DIR = Path("data/pages")
DEFAULT_LONG_EDGE = 1568  # pixels on the longest side of each page picture
DEFAULT_MAX_PAGES = 60
DEFAULT_JPEG_QUALITY = 90  # decision 46 (amended): JPEG keeps uploads 3-5x smaller than PNG
TEXT_CLEANUP = 1  # raise when clean_text() changes, so saved pages are rebuilt (decisions 81, 83)

SPLIT_WORD_MARK = "\ufffe"  # PDFium's stand-in for a hyphen at a line end (finding 14)
_WORD_GAPS = re.compile(r"([ \t]+)")  # spaces and tabs only: str.split() would also split on control codes


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
    text: str  # cleaned text; empty for image-only pages
    image_path: Path
    text_complete: bool = True  # False when junk words were dropped from this page (decision 81)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True)
class DeckPages:
    deck_sha256: str
    long_edge: int
    jpeg_quality: int
    pages: tuple[Page, ...]


def _page_files(folder: Path, number: int) -> tuple[Path, Path]:
    return folder / f"page-{number:03d}.jpg", folder / f"page-{number:03d}.txt"


def _raw_file(folder: Path, number: int) -> Path:
    return folder / f"page-{number:03d}.raw.txt"  # the text exactly as PDFium gave it


def _is_junk(word: str) -> bool:
    """A word with any control code in it (fonts without a character map, finding 12)."""
    return any(unicodedata.category(c) == "Cc" for c in word)


def clean_text(raw: str) -> tuple[str, int]:
    """Return (cleaned text, number of junk words dropped).

    Decision 83: PDFium's split-word mark goes back to what is printed, a hyphen and a line break.
    Decision 81: every word holding a control code is dropped; clean words and lines stay as they were.
    Tab and line break are spacing, not junk.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace(SPLIT_WORD_MARK, "-\n")
    dropped = 0
    lines = []
    for line in text.split("\n"):
        if not _is_junk(line.replace("\t", " ")):
            lines.append(line)  # nothing to drop: keep the line exactly
            continue
        pieces = _WORD_GAPS.split(line)  # words at even positions, the gaps between them at odd ones
        kept = []
        for word in pieces[0::2]:
            if not word:
                continue
            if _is_junk(word):
                dropped += 1
            else:
                kept.append(word)
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines), dropped


def _load(folder: Path, deck_sha256: str, long_edge: int, jpeg_quality: int) -> DeckPages | None:
    """Return the cached pages if the folder is complete and matches, else None."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {"deck_sha256": deck_sha256, "long_edge": long_edge, "jpeg_quality": jpeg_quality}
    if any(manifest.get(key) != value for key, value in expected.items()):
        return None  # a different deck, size or quality (older PNG folders too): render again
    if manifest.get("text_cleanup") != TEXT_CLEANUP:
        return None  # saved before the current cleaning rules: render again (free)
    incomplete = set(manifest.get("text_incomplete_pages", []))
    pages = []
    for number in range(1, manifest.get("page_count", 0) + 1):
        jpg, txt = _page_files(folder, number)
        if not (jpg.exists() and txt.exists() and _raw_file(folder, number).exists()):
            return None
        pages.append(Page(number, txt.read_text(encoding="utf-8"), jpg, number not in incomplete))
    return DeckPages(deck_sha256, long_edge, jpeg_quality, tuple(pages)) if pages else None


def _render(pdf_bytes: bytes, folder: Path, long_edge: int, max_pages: int, jpeg_quality: int) -> tuple[int, list[int]]:
    """Write every page's picture, raw text and cleaned text into `folder`.
    Returns the page count and the numbers of pages that lost junk words."""
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
        incomplete = []
        for index in range(count):
            page = pdf[index]
            try:
                width, height = page.get_size()
                scale = long_edge / max(width, height)
                jpg, txt = _page_files(folder, index + 1)
                picture = page.render(scale=scale).to_pil().convert("RGB")
                picture.save(jpg, format="JPEG", quality=jpeg_quality)
                raw = page.get_textpage().get_text_range()
                _raw_file(folder, index + 1).write_text(raw, encoding="utf-8", newline="")
                text, dropped = clean_text(raw)
                txt.write_text(text, encoding="utf-8", newline="")
                if dropped:
                    incomplete.append(index + 1)
            except DeckError:
                raise
            except Exception as e:  # one bad page fails the whole deck (decision 48)
                raise DeckError("unreadable", f"page {index + 1}: {e}") from e
            finally:
                page.close()
        return count, incomplete
    finally:
        pdf.close()


def deck_to_pages(
    store: RawStore,
    deck_sha256: str,
    root: Path = DEFAULT_PAGES_DIR,
    long_edge: int = DEFAULT_LONG_EDGE,
    max_pages: int = DEFAULT_MAX_PAGES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> DeckPages:
    """Turn a stored deck into pages, rendering it once and reusing the result after that."""
    folder = Path(root) / deck_sha256 / f"{long_edge}px"
    cached = _load(folder, deck_sha256, long_edge, jpeg_quality)
    if cached:
        return cached

    pdf_bytes = store.load(deck_sha256)  # also checks the fingerprint
    folder.parent.mkdir(parents=True, exist_ok=True)
    if folder.exists():  # outdated or incomplete; the raw file can always rebuild it
        shutil.rmtree(folder)

    tmp = Path(tempfile.mkdtemp(dir=folder.parent, prefix=".tmp-"))
    try:
        count, incomplete = _render(pdf_bytes, tmp, long_edge, max_pages, jpeg_quality)
        manifest = {
            "deck_sha256": deck_sha256,
            "long_edge": long_edge,
            "jpeg_quality": jpeg_quality,
            "page_count": count,
            "text_cleanup": TEXT_CLEANUP,
            "text_incomplete_pages": incomplete,
            "renderer": f"pypdfium2 {pdfium.version.PYPDFIUM_INFO}",
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, folder)  # the folder appears whole, or not at all
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return _load(folder, deck_sha256, long_edge, jpeg_quality)