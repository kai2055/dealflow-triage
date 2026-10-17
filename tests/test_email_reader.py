"""Tests for the email reader, using small emails built inside the test."""

from email.message import EmailMessage

import pytest

from triage.intake import email_reader
from triage.intake.email_reader import detect_type, find_links, read_eml
from triage.storage import RawStore

PDF_BYTES = b"%PDF-1.4 a tiny pretend deck"


def build_email(body="Hi, deck attached.", html=None, attach=None, date="Fri, 16 Oct 2026 21:56:00 +0200"):
    msg = EmailMessage()
    msg["From"] = "Founder <founder@example.com>"
    msg["To"] = "dealflowtriage@gmail.com"
    msg["Subject"] = "Acme: raising a seed round"
    msg["Message-ID"] = "<abc123@example.com>"
    if date:
        msg["Date"] = date
    if body is not None:
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")
    for filename, data in attach or []:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes()


@pytest.fixture
def store(tmp_path):
    return RawStore(tmp_path)


def test_headers_and_body_are_read(store):
    pitch = read_eml(build_email(), store)
    assert pitch.sender == "Founder <founder@example.com>"
    assert pitch.subject == "Acme: raising a seed round"
    assert pitch.message_id == "<abc123@example.com>"
    assert pitch.body_text == "Hi, deck attached."
    assert pitch.received_at.isoformat() == "2026-10-16T21:56:00+02:00"


def test_original_email_is_kept_byte_for_byte(store):
    raw = build_email()
    pitch = read_eml(raw, store)
    assert store.load(pitch.raw_hash) == raw


def test_pdf_attachment_is_saved_and_supported(store):
    pitch = read_eml(build_email(attach=[("deck.pdf", PDF_BYTES)]), store)
    [deck] = pitch.attachments
    assert (deck.filename, deck.detected_type, deck.supported) == ("deck.pdf", "pdf", True)
    assert store.load(deck.sha256) == PDF_BYTES


def test_type_comes_from_content_not_name(store):
    pitch = read_eml(build_email(attach=[("Fuzzy.ai", PDF_BYTES), ("fake.pdf", b"not a pdf")]), store)
    real, fake = pitch.attachments
    assert real.detected_type == "pdf" and real.supported
    assert fake.detected_type == "unknown" and not fake.supported


def test_oversized_pdf_is_stored_but_unsupported(store, monkeypatch):
    monkeypatch.setattr(email_reader, "MAX_ATTACHMENT_BYTES", 10)
    pitch = read_eml(build_email(attach=[("big.pdf", PDF_BYTES)]), store)
    [deck] = pitch.attachments
    assert not deck.supported
    assert store.load(deck.sha256) == PDF_BYTES


def test_plain_text_is_preferred_over_html(store):
    pitch = read_eml(build_email(body="Plain version", html="<p>HTML version</p>"), store)
    assert pitch.body_text == "Plain version"


def test_html_only_email_is_converted_to_text(store):
    html = "<html><style>p {color: red}</style><p>Hello</p><p>See https://example.com/deck.</p></html>"
    pitch = read_eml(build_email(body=None, html=html), store)
    assert pitch.body_text == "Hello\nSee https://example.com/deck."
    assert pitch.links == ["https://example.com/deck"]


def test_missing_date_falls_back_to_now(store):
    pitch = read_eml(build_email(date=None), store)
    assert pitch.received_at.tzinfo is not None


def test_links_are_found_cleaned_and_deduplicated():
    text = "Deck: https://docsend.com/view/abc, or https://docsend.com/view/abc. Thanks!"
    assert find_links(text) == ["https://docsend.com/view/abc"]


def test_detect_type():
    assert detect_type(PDF_BYTES) == "pdf"
    assert detect_type(b"PK\x03\x04 a zip file") == "unknown"