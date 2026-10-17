"""Email reader: turns one .eml file into a pitch record."""

import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from triage.models import Attachment, RawPitch, Source
from triage.storage import RawStore

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class _TextFromHTML(HTMLParser):
    """Collects the visible text of an HTML email, skipping scripts and styles."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextFromHTML()
    parser.feed(html)
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def detect_type(data: bytes) -> str:
    """Decide the file type from its first bytes, never from its name."""
    return "pdf" if data.startswith(b"%PDF-") else "unknown"


def find_links(text: str) -> list[str]:
    links: list[str] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        if url not in links:
            links.append(url)
    return links


def _clean(header) -> str | None:
    if header is None:
        return None
    value = str(header).strip()
    return value or None


def _received_at(msg) -> datetime:
    try:
        sent = msg["Date"].datetime
    except Exception:
        sent = None
    if sent is None:
        return datetime.now(timezone.utc)
    if sent.tzinfo is None:
        return sent.replace(tzinfo=timezone.utc)
    return sent


def _body_text(msg) -> str | None:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content().strip() or None
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text(html.get_content()) or None
    return None


def read_eml(raw: bytes, store: RawStore, source: Source = Source.FOLDER) -> RawPitch:
    """Save the original email and its attachments, and return the pitch record."""
    raw_hash = store.save(raw)
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = _body_text(msg)

    attachments = []
    for part in msg.walk():
        if part.is_multipart() or not part.is_attachment():
            continue
        data = part.get_payload(decode=True) or b""
        kind = detect_type(data)
        attachments.append(Attachment(
            sha256=store.save(data),
            filename=part.get_filename() or "unnamed",
            detected_type=kind,
            size_bytes=len(data),
            supported=(kind == "pdf" and len(data) <= MAX_ATTACHMENT_BYTES),
        ))

    return RawPitch(
        source=source,
        raw_hash=raw_hash,
        message_id=_clean(msg["Message-ID"]),
        received_at=_received_at(msg),
        sender=_clean(msg["From"]),
        subject=_clean(msg["Subject"]),
        body_text=body,
        attachments=attachments,
        links=find_links(body or ""),
    )