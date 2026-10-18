"""Tests for the bake-off: one yes for the plan, a hard spending stop, skip what already succeeded."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_extract import GOOD, FakeModel
from test_pages import make_pdf
from triage import db
from triage.bakeoff import PlanError, load_plan, run_plan
from triage.cost import Price
from triage.model import CallRecord, ModelCallError, ModelResult, ModelSettings
from triage.models import Attachment, RawPitch
from triage.storage import RawStore

NOW = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)
BASE = ModelSettings(base_url="https://openrouter.ai/api/v1", model="unused", api_key="k")
PRICES = {"a/strong": Price(2.0, 10.0), "b/strong": Price(2.0, 10.0)}
PLAN_TEXT = """
prompt = "pitch_card_v1"
decks = ["Cyclica", "Gridcure"]

[[model]]
name = "a/strong"
temperature = "none"

[[model]]
name = "b/strong"
temperature = "0"
"""


class PricedFake(FakeModel):
    """A fake model whose receipt reports a chosen cost."""

    def __init__(self, cost=0.01, **kwargs):
        super().__init__(**kwargs)
        self.cost = cost

    def read(self, instructions, pages, schema_name, schema):
        result = super().read(instructions, pages, schema_name, schema)
        r = result.record
        return ModelResult(result.raw_text, CallRecord(r.requested_model, r.answered_model, r.provider,
                                                       r.called_at, r.output_mode, r.temperature,
                                                       r.data_collection, 3000, 400, self.cost))


@pytest.fixture
def world(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    store = RawStore(tmp_path / "raw")
    for name, text in [("Cyclica.pdf", "CYCLICA"), ("Gridcure.pdf", "GridCure")]:
        deck = make_pdf([text, "Seeking: CAD$20M in a Series B round"])
        sha = store.save(deck)
        db.insert_pitch(conn, RawPitch(source="folder", raw_hash=sha, received_at=NOW, attachments=[
            Attachment(sha256=sha, filename=name, detected_type="pdf", size_bytes=len(deck), supported=True)]))
    plan_file = tmp_path / "plan.toml"
    plan_file.write_text(PLAN_TEXT)
    yield conn, store, plan_file, tmp_path / "pages"
    conn.close()


def run(world, answer="yes", prices=lambda: PRICES, again=False, make_model=lambda s: PricedFake(answer=GOOD)):
    conn, store, plan_file, pages = world
    made, lines = [], []

    def make_client(settings):
        model = make_model(settings)
        model.settings = settings
        made.append(model)
        return model

    code = run_plan(conn, store, load_plan(plan_file), BASE, pages, again, lambda q: (lines.append(q), answer)[1],
                    prices, make_client, lines.append)
    return code, made, lines


def count(conn, sql="SELECT COUNT(*) FROM extraction"):
    return conn.execute(sql).fetchone()[0]


def test_plan_file_is_read_with_per_model_temperature(world):
    plan = load_plan(world[2])
    assert plan.decks == ("Cyclica", "Gridcure")
    assert [(m.name, m.temperature) for m in plan.models] == [("a/strong", None), ("b/strong", 0.0)]


def test_bad_plan_is_refused(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('decks = ["Cyclica"]\n[[model]]\nname = "a/strong"\ntemperature = "cold"\n')
    with pytest.raises(PlanError, match="temperature"):
        load_plan(bad)
    bad.write_text('decks = ["Cyclica"]\n')
    with pytest.raises(PlanError, match="model"):
        load_plan(bad)


def test_one_yes_runs_every_model_on_every_deck_as_bakeoff(world):
    conn = world[0]
    code, made, lines = run(world)
    assert code == 0 and len(made) == 4
    questions = [line for line in lines if "Type yes" in line]
    assert len(questions) == 1 and "4 runs" in questions[0]  # asked once, for the whole plan
    assert count(conn, "SELECT COUNT(*) FROM extraction WHERE purpose = 'bakeoff'") == 4
    assert count(conn, "SELECT COUNT(*) FROM pitch WHERE state = 'received'") == 2  # states untouched
    assert {(m.settings.model, m.settings.temperature) for m in made} == {("a/strong", None), ("b/strong", 0.0)}


def test_anything_but_yes_sends_nothing(world):
    code, made, lines = run(world, answer="ok")
    assert code == 3 and made == [] and count(world[0]) == 0
    assert "Cancelled. Nothing was sent." in lines


def test_a_model_missing_from_the_price_list_stops_the_whole_plan(world):
    code, made, lines = run(world, prices=lambda: {"a/strong": Price(2.0, 10.0)})
    assert code == 3 and made == [] and count(world[0]) == 0
    assert not any("Type yes" in line for line in lines)  # refused before asking


def test_real_spending_past_the_agreed_limit_stops_the_batch(world):
    code, made, lines = run(world, make_model=lambda s: PricedFake(cost=5.0, answer=GOOD))
    assert code == 4 and len(made) == 1  # the first run cost $5, far past the limit: nothing more starts
    assert any(line.startswith("Stopped:") for line in lines)


def test_second_run_skips_what_already_succeeded_and_again_repeats_it(world):
    run(world)
    code, made, lines = run(world)
    assert code == 0 and made == [] and count(world[0]) == 4
    assert any("already succeeded" in line for line in lines)
    code, made, _ = run(world, again=True)
    assert len(made) == 4 and count(world[0]) == 8  # repeated on purpose, nothing overwritten


def test_failed_combinations_are_tried_again_next_time(world):
    run(world, make_model=lambda s: PricedFake(error=ModelCallError("unavailable", "down")))
    code, made, _ = run(world)
    assert code == 0 and len(made) == 4


def test_deck_missing_from_the_register_stops_before_anything_is_sent(world):
    conn, store, plan_file, pages = world
    plan_file.write_text(PLAN_TEXT.replace('"Gridcure"', '"Lumific"'))
    with pytest.raises(PlanError, match="Lumific"):
        run(world)
    assert count(conn) == 0


def test_round1_plan_in_the_repo_is_valid():
    plan = load_plan(Path(__file__).resolve().parents[1] / "bakeoff" / "round1.toml")
    assert plan.decks == ("DecisiveHealth", "Cyclica", "Gridcure")
    assert [m.temperature for m in plan.models] == [None, None, 0.0]