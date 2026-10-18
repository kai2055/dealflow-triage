"""Tests for the extraction step, with a fake model (B6): no network, no cost, and every hard path on purpose."""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from test_pages import make_pdf
from triage.card import PitchCard
from triage.db import connect, insert_pitch
from triage.extract import extract_pitch, load_instructions, strict_layout
from triage.model import CallRecord, ModelCallError, ModelResult, ModelSettings
from triage.models import Attachment, RawPitch
from triage.storage import RawStore

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)
SETTINGS = ModelSettings(base_url="https://openrouter.example/api/v1", model="fake/model", api_key="k")
DECK_PAGES = ["CYCLICA Toronto, Ontario", "Seeking: CAD$20M in a Series B round"]


class FakeModel:
    """Stands in for a real model: gives a scripted answer (or error) and remembers what it was sent."""

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def read(self, instructions, pages, schema_name, schema):
        self.calls.append({"instructions": instructions, "pages": pages, "schema": schema})
        if self.error:
            raise self.error
        record = CallRecord("fake/model", "fake/model-2026", "FakeProvider", NOW.isoformat(),
                            "strict", 0.0, "deny", 3000, 400, 0.01)
        return ModelResult(self.answer, record)


def fact(value, quote, page):
    return {"value": value, "evidence": {"quote": quote, "page": page}}


def empty_fact():
    return {"value": None, "evidence": None}


def answer(**facts):
    """A full answer in the strict layout: every field present, unfilled ones null."""
    card = {name: empty_fact() for name in ["company_name", "website", "one_liner", "sector", "stage",
                                            "round_size", "headquarters", "product", "ai_role"]}
    card.update(team=[], traction=[])
    card.update(facts)
    return json.dumps(card)


GOOD = answer(company_name=fact("Cyclica", "CYCLICA", 1),
              headquarters=fact("Toronto, Ontario", "Toronto, Ontario", 1),
              round_size=fact({"amount": 20000000, "currency": "CAD", "status": "sought"},
                              "Seeking: CAD$20M in a Series B round", 2))


@pytest.fixture
def setup(tmp_path):
    conn = connect(tmp_path / "test.db")
    store = RawStore(tmp_path / "raw")

    def add_pitch(deck_bytes=None):
        attachments = []
        if deck_bytes is not None:
            sha = store.save(deck_bytes)
            attachments = [Attachment(sha256=sha, filename="Cyclica.pdf", detected_type="pdf",
                                      size_bytes=len(deck_bytes), supported=True)]
        raw_hash = store.save(deck_bytes if deck_bytes is not None else b"email without a deck")
        pitch_id, _ = insert_pitch(conn, RawPitch(source="folder", raw_hash=raw_hash, received_at=NOW,
                                                  attachments=attachments))
        return pitch_id

    def run(pitch_id, model, purpose="pipeline"):
        return extract_pitch(conn, store, model, SETTINGS, pitch_id, purpose, pages_root=tmp_path / "pages")

    yield conn, add_pitch, run
    conn.close()


def state_of(conn, pitch_id):
    return conn.execute("SELECT state FROM pitch WHERE pitch_id = ?", (pitch_id,)).fetchone()["state"]


def only_attempt(conn):
    [row] = conn.execute("SELECT * FROM extraction").fetchall()
    return row


# --- The layout sent to the model (decision 65) ---

def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_strict_layout_has_no_rules_or_cross_references():
    nodes = list(walk(strict_layout()))
    refused = {"$ref", "$defs", "pattern", "minLength", "minimum", "exclusiveMinimum", "default", "title"}
    assert not any(refused & node.keys() for node in nodes)
    for node in nodes:
        if "properties" in node:
            assert node["required"] == list(node["properties"])
            assert node["additionalProperties"] is False


def test_answer_in_the_strict_layout_becomes_a_card_and_the_card_still_enforces_its_rules():
    assert PitchCard.model_validate_json(GOOD).completeness() == round(3 / 11, 2)
    with pytest.raises(ValidationError):  # page 0: allowed by the simple layout, refused by the card
        PitchCard.model_validate_json(answer(company_name=fact("Cyclica", "CYCLICA", 0)))


def test_instructions_v1_exist_and_name_the_page_labels():
    assert "--- Page N ---" in load_instructions("pitch_card_v1")


# --- The extraction step ---

def test_good_answer_is_saved_with_receipt_and_quote_counts(setup):
    conn, add_pitch, run = setup
    pitch_id = add_pitch(make_pdf(DECK_PAGES))
    attempt = run(pitch_id, FakeModel(GOOD))
    assert attempt.outcome == "extracted" and attempt.card.company_name.value == "Cyclica"
    row = only_attempt(conn)
    assert (row["purpose"], row["prompt_version"], row["requested_model"]) == ("pipeline", "pitch_card_v1", "fake/model")
    assert (row["answered_model"], row["provider"], row["cost"]) == ("fake/model-2026", "FakeProvider", 0.01)
    assert (row["quotes_verified"], row["quotes_bad"], row["quotes_unverifiable"]) == (3, 0, 0)
    assert json.loads(row["quote_report_json"])[0]["status"] == "verified"
    assert state_of(conn, pitch_id) == "extracted"


def test_model_receives_v1_instructions_the_strict_layout_and_every_page(setup):
    conn, add_pitch, run = setup
    model = FakeModel(GOOD)
    run(add_pitch(make_pdf(DECK_PAGES)), model)
    [call] = model.calls
    assert call["instructions"] == load_instructions("pitch_card_v1")
    assert call["schema"] == strict_layout()
    assert [p.number for p in call["pages"]] == [1, 2]


def test_bakeoff_attempt_is_saved_but_never_moves_the_pitch(setup):
    conn, add_pitch, run = setup
    pitch_id = add_pitch(make_pdf(DECK_PAGES))
    run(pitch_id, FakeModel(GOOD), purpose="bakeoff")
    run(pitch_id, FakeModel(error=ModelCallError("unavailable", "down")), purpose="bakeoff")
    assert conn.execute("SELECT COUNT(*) FROM extraction").fetchone()[0] == 2  # nothing overwritten
    assert state_of(conn, pitch_id) == "received"


def test_bad_quote_is_counted_but_the_card_is_kept(setup):
    conn, add_pitch, run = setup
    pitch_id = add_pitch(make_pdf(DECK_PAGES))
    invented = answer(company_name=fact("Cyclica", "CYCLICA", 1),
                      traction=[{"kind": "revenue", "text": "$5M revenue",
                                 "evidence": {"quote": "$5M in revenue", "page": 2}}])
    attempt = run(pitch_id, FakeModel(invented))
    assert attempt.outcome == "extracted"
    assert (only_attempt(conn)["quotes_verified"], only_attempt(conn)["quotes_bad"]) == (1, 1)


def test_answer_that_breaks_the_card_fails_and_keeps_the_raw_text(setup):
    conn, add_pitch, run = setup
    pitch_id = add_pitch(make_pdf(DECK_PAGES))
    broken = answer(company_name={"value": "Cyclica", "evidence": None})  # a value without a quote
    attempt = run(pitch_id, FakeModel(broken))
    row = only_attempt(conn)
    assert (attempt.failure, row["outcome"], row["raw_text"]) == ("invalid_card", "failed", broken)
    assert row["provider"] == "FakeProvider"  # the receipt is kept too
    assert state_of(conn, pitch_id) == "failed"


def test_model_error_is_saved_with_what_was_asked(setup):
    conn, add_pitch, run = setup
    pitch_id = add_pitch(make_pdf(DECK_PAGES))
    attempt = run(pitch_id, FakeModel(error=ModelCallError("unavailable", "still failing")))
    row = only_attempt(conn)
    assert attempt.failure == "model_unavailable"
    assert (row["requested_model"], row["answered_model"], row["raw_text"]) == ("fake/model", None, None)


def test_broken_deck_fails_before_the_model_is_called(setup):
    conn, add_pitch, run = setup
    model = FakeModel(GOOD)
    attempt = run(add_pitch(make_pdf(DECK_PAGES)[:60]), model)
    assert attempt.failure == "deck_unreadable"
    assert model.calls == []  # no model call, no cost


def test_pitch_without_a_deck_fails_as_no_deck(setup):
    conn, add_pitch, run = setup
    model = FakeModel(GOOD)
    attempt = run(add_pitch(None), model)
    assert attempt.failure == "no_deck" and model.calls == []