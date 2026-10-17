"""Tests for the folder adapter: each kind of file goes to the right reader."""

import os
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from triage.intake.folder import read_folder
from triage.models import PitchState
from triage.storage import RawStore

PDF_BYTES = b"%PDF-1.4 a tiny pretend deck"


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    folder.mkdir()
    return folder


@pytest.fixture
def store(tmp_path):
    return RawStore(tmp_path / "raw")


def test_pdf_is_recognised_by_content_even_without_extension(inbox, store):
    (inbox / "Fuzzy.ai").write_bytes(PDF_BYTES)
    [item] = read_folder(inbox, store)
    [deck] = item.pitch.attachments
    assert deck.detected_type == "pdf" and deck.supported
    assert item.pitch.raw_hash == deck.sha256
    assert store.load(deck.sha256) == PDF_BYTES


def test_eml_goes_to_the_email_reader(inbox, store):
    msg = EmailMessage()
    msg["Subject"] = "Acme: raising a seed round"
    msg["Date"] = "Fri, 16 Oct 2026 21:56:00 +0200"
    msg.set_content("Hi, deck attached.")
    (inbox / "pitch.eml").write_bytes(msg.as_bytes())
    [item] = read_folder(inbox, store)
    assert item.pitch.subject == "Acme: raising a seed round"


def test_txt_becomes_a_text_pitch(inbox, store):
    text = "Hi, we build AI for warehouses. Deck: https://example.com/deck"
    (inbox / "linkedin-pitch.txt").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    [item] = read_folder(inbox, store)
    assert item.pitch.body_text == text
    assert item.pitch.links == ["https://example.com/deck"]
    assert item.pitch.attachments == []


def test_unknown_file_is_kept_and_marked_failed(inbox, store):
    (inbox / "fake.pdf").write_bytes(b"this is not a pdf")
    [item] = read_folder(inbox, store)
    assert item.pitch.state == PitchState.FAILED
    assert item.note == "unsupported file type: fake.pdf"
    assert store.load(item.pitch.raw_hash) == b"this is not a pdf"


def test_system_files_and_subfolders_are_ignored(inbox, store):
    (inbox / "desktop.ini").write_bytes(b"[settings]")
    (inbox / ".hidden").write_bytes(b"secret")
    (inbox / "holdout").mkdir()
    (inbox / "holdout" / "test-deck.pdf").write_bytes(PDF_BYTES)
    assert read_folder(inbox, store) == []


def test_files_are_read_in_alphabetical_order(inbox, store):
    for name in ["c.txt", "A.txt", "b.txt"]:
        (inbox / name).write_bytes(name.encode())
    names = [item.path.name for item in read_folder(inbox, store)]
    assert names == ["A.txt", "b.txt", "c.txt"]


def test_loose_file_uses_its_modified_time(inbox, store):
    path = inbox / "deck.pdf"
    path.write_bytes(PDF_BYTES)
    when = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    os.utime(path, (when.timestamp(), when.timestamp()))
    [item] = read_folder(inbox, store)
    assert item.pitch.received_at == when