"""Tests for raw storage: files come back identical, duplicates are stored once, damage is caught."""

import pytest

from triage.storage import CorruptFileError, RawStore, fingerprint


def test_saved_file_loads_back_identical(tmp_path):
    store = RawStore(tmp_path)
    data = b"%PDF-1.4 example deck"
    sha = store.save(data)
    assert store.load(sha) == data


def test_same_content_is_stored_once(tmp_path):
    store = RawStore(tmp_path)
    first = store.save(b"same pitch")
    second = store.save(b"same pitch")
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


def test_different_content_gets_different_fingerprint(tmp_path):
    store = RawStore(tmp_path)
    assert store.save(b"pitch A") != store.save(b"pitch B")


def test_damaged_file_is_detected(tmp_path):
    store = RawStore(tmp_path)
    sha = store.save(b"original content")
    (tmp_path / sha).write_bytes(b"tampered content")
    with pytest.raises(CorruptFileError):
        store.load(sha)


def test_invalid_fingerprint_is_refused(tmp_path):
    store = RawStore(tmp_path)
    with pytest.raises(ValueError):
        store.load("../../secrets.txt")


def test_no_temporary_files_left_behind(tmp_path):
    store = RawStore(tmp_path)
    store.save(b"some pitch")
    assert not any(p.name.startswith(".tmp-") for p in tmp_path.iterdir())


def test_fingerprint_matches_known_sha256():
    empty_file_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert fingerprint(b"") == empty_file_sha256