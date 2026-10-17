"""Tests for the ingest command: the whole intake, end to end, in temporary folders."""

from email.message import EmailMessage

import pytest

from triage import cli, db

PDF_BYTES = b"%PDF-1.4 a tiny pretend deck"


@pytest.fixture
def paths(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return {"inbox": inbox, "db": tmp_path / "test.db", "raw": tmp_path / "raw"}


def run(paths):
    return cli.main(["ingest", "--folder", str(paths["inbox"]),
                     "--db", str(paths["db"]), "--raw", str(paths["raw"])])


def count(paths, sql):
    conn = db.connect(paths["db"])
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_every_file_is_ingested(paths):
    (paths["inbox"] / "a.pdf").write_bytes(PDF_BYTES)
    (paths["inbox"] / "b.pdf").write_bytes(PDF_BYTES + b" another deck")
    assert run(paths) == 0
    assert count(paths, "SELECT COUNT(*) FROM pitch") == 2


def test_running_twice_changes_nothing(paths):
    (paths["inbox"] / "a.pdf").write_bytes(PDF_BYTES)
    run(paths)
    assert run(paths) == 0
    assert count(paths, "SELECT COUNT(*) FROM pitch") == 1
    assert count(paths, "SELECT COUNT(*) FROM state_history") == 1


def test_same_deck_in_a_new_email_is_flagged(paths):
    (paths["inbox"] / "a-deck.pdf").write_bytes(PDF_BYTES)
    msg = EmailMessage()
    msg["Subject"] = "Acme pitch"
    msg.set_content("Deck attached.")
    msg.add_attachment(PDF_BYTES, maintype="application", subtype="pdf", filename="deck.pdf")
    (paths["inbox"] / "b-email.eml").write_bytes(msg.as_bytes())
    run(paths)
    assert count(paths, "SELECT COUNT(*) FROM pitch WHERE possible_duplicate_of IS NOT NULL") == 1


def test_unsupported_file_is_recorded_with_its_reason(paths):
    (paths["inbox"] / "report.docx").write_bytes(b"PK\x03\x04 not a deck")
    assert run(paths) == 0
    assert count(paths, "SELECT COUNT(*) FROM pitch WHERE state = 'failed'") == 1
    assert count(paths, "SELECT COUNT(*) FROM state_history "
                        "WHERE reason = 'unsupported file type: report.docx'") == 1


def test_one_broken_file_does_not_stop_the_others(paths, monkeypatch):
    (paths["inbox"] / "a-broken.pdf").write_bytes(PDF_BYTES + b" broken")
    (paths["inbox"] / "b-fine.pdf").write_bytes(PDF_BYTES)
    real_read_file = cli.read_file

    def read_file_that_breaks(path, store):
        if path.name == "a-broken.pdf":
            raise OSError("disk read failed")
        return real_read_file(path, store)

    monkeypatch.setattr(cli, "read_file", read_file_that_breaks)
    assert run(paths) == 1
    assert count(paths, "SELECT COUNT(*) FROM pitch") == 1


def test_missing_folder_is_reported(paths, tmp_path):
    exit_code = cli.main(["ingest", "--folder", str(tmp_path / "nope"),
                          "--db", str(paths["db"]), "--raw", str(paths["raw"])])
    assert exit_code == 2