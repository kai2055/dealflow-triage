"""Tests for the pitch register: duplicates are refused, rules are enforced, history is kept."""

import sqlite3
from datetime import datetime, timezone

import pytest

from triage.db import connect, insert_pitch, mark_possible_duplicate, set_state
from triage.models import Attachment, PitchState, RawPitch

NOW = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def make_pitch(raw_hash="a" * 64, message_id=None, **extra):
    return RawPitch(source="folder", raw_hash=raw_hash, message_id=message_id,
                    received_at=NOW, **extra)


def test_new_pitch_is_stored_with_first_history_row(conn):
    pitch_id, inserted = insert_pitch(conn, make_pitch())
    assert inserted
    row = conn.execute("SELECT state FROM pitch WHERE pitch_id = ?", (pitch_id,)).fetchone()
    assert row["state"] == "received"
    history = conn.execute("SELECT from_state, to_state, reason FROM state_history").fetchall()
    assert [tuple(h) for h in history] == [(None, "received", "ingested")]


def test_same_file_twice_is_refused(conn):
    first_id, _ = insert_pitch(conn, make_pitch())
    second_id, inserted = insert_pitch(conn, make_pitch())
    assert not inserted
    assert second_id == first_id
    assert conn.execute("SELECT COUNT(*) FROM pitch").fetchone()[0] == 1


def test_same_email_fetched_twice_is_refused(conn):
    insert_pitch(conn, make_pitch(raw_hash="a" * 64, message_id="<abc@mail>"))
    _, inserted = insert_pitch(conn, make_pitch(raw_hash="b" * 64, message_id="<abc@mail>"))
    assert not inserted


def test_files_without_message_id_are_not_confused(conn):
    _, first = insert_pitch(conn, make_pitch(raw_hash="a" * 64))
    _, second = insert_pitch(conn, make_pitch(raw_hash="b" * 64))
    assert first and second


def test_attachments_are_stored(conn):
    deck = Attachment(sha256="c" * 64, filename="deck.pdf", detected_type="pdf",
                      size_bytes=1000, supported=True)
    pitch_id, _ = insert_pitch(conn, make_pitch(attachments=[deck]))
    row = conn.execute("SELECT filename, supported FROM attachment WHERE pitch_id = ?",
                       (pitch_id,)).fetchone()
    assert tuple(row) == ("deck.pdf", 1)


def test_state_change_is_recorded_in_history(conn):
    pitch_id, _ = insert_pitch(conn, make_pitch())
    set_state(conn, pitch_id, PitchState.EXTRACTED, reason="pitch card built")
    assert conn.execute("SELECT state FROM pitch").fetchone()["state"] == "extracted"
    last = conn.execute("SELECT from_state, to_state FROM state_history ORDER BY id DESC").fetchone()
    assert tuple(last) == ("received", "extracted")


def test_state_change_for_unknown_pitch_is_refused(conn):
    with pytest.raises(KeyError):
        set_state(conn, "no-such-pitch", PitchState.EXTRACTED)


def test_database_refuses_an_invalid_state(conn):
    pitch_id, _ = insert_pitch(conn, make_pitch())
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("UPDATE pitch SET state = 'banana' WHERE pitch_id = ?", (pitch_id,))


def test_duplicate_flag_must_point_to_a_real_pitch(conn):
    first_id, _ = insert_pitch(conn, make_pitch(raw_hash="a" * 64))
    second_id, _ = insert_pitch(conn, make_pitch(raw_hash="b" * 64))
    mark_possible_duplicate(conn, second_id, first_id)
    with pytest.raises(sqlite3.IntegrityError):
        mark_possible_duplicate(conn, second_id, "no-such-pitch")


def test_data_survives_reconnecting(tmp_path):
    path = tmp_path / "test.db"
    first = connect(path)
    insert_pitch(first, make_pitch())
    first.close()
    second = connect(path)
    assert second.execute("SELECT COUNT(*) FROM pitch").fetchone()[0] == 1
    second.close()

def test_old_database_gets_the_new_column_and_keeps_its_pitches(tmp_path):
    path = tmp_path / "old.db"
    first = connect(path)
    insert_pitch(first, make_pitch())
    first.execute("ALTER TABLE extraction DROP COLUMN quotes_not_in_text")  # a file made before decision 84
    first.close()
    upgraded = connect(path)
    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(extraction)")}
    assert "quotes_not_in_text" in columns
    assert upgraded.execute("SELECT COUNT(*) FROM pitch").fetchone()[0] == 1
    upgraded.close()
    connect(path).close()  # a second upgrade finds nothing to add and does not fail