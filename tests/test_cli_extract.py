"""Tests for `triage extract`: finds the deck by name, asks before spending, and sends nothing without "yes"."""

from datetime import datetime, timezone

import pytest

from test_extract import GOOD, FakeModel
from test_pages import make_pdf
from triage import cli, db
from triage.cost import Price, PriceUnavailable
from triage.models import Attachment, RawPitch
from triage.storage import RawStore

NOW = datetime(2026, 10, 18, 21, 0, tzinfo=timezone.utc)
PRICES = {"anthropic/claude-sonnet-5.5": Price(2.0, 10.0), "google/gemini-4-argon": Price(2.0, 10.0)}
DECK = make_pdf(["CYCLICA Toronto, Ontario", "Seeking: CAD$20M in a Series B round"])


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ["TRIAGE_BASE_URL", "TRIAGE_MODEL", "TRIAGE_API_KEY", "TRIAGE_TEMPERATURE"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def world(tmp_path):
    paths = {"db": tmp_path / "test.db", "raw": tmp_path / "raw", "pages": tmp_path / "pages",
             "env": tmp_path / ".env"}
    paths["env"].write_text("TRIAGE_BASE_URL=https://openrouter.ai/api/v1\n"
                            "TRIAGE_MODEL=anthropic/claude-sonnet-5.5\nTRIAGE_API_KEY=k\n")
    return paths


def add_deck(paths, filename="Cyclica.pdf", deck=DECK):
    store = RawStore(paths["raw"])
    sha = store.save(deck)
    conn = db.connect(paths["db"])
    pitch_id, _ = db.insert_pitch(conn, RawPitch(
        source="folder", raw_hash=store.save(deck + filename.encode()), received_at=NOW,
        attachments=[Attachment(sha256=sha, filename=filename, detected_type="pdf",
                                size_bytes=len(deck), supported=True)]))
    conn.close()
    return pitch_id


def run(paths, answer="yes", prices=lambda: PRICES, deck="Cyclica", model=None, temperature=None):
    made = []

    def make_client(settings):
        made.append(FakeModel(GOOD))
        made[-1].settings = settings
        return made[-1]

    questions = []

    def ask(question):
        questions.append(question)
        return answer

    code = cli.extract_command(deck, model, temperature, "bakeoff", "pitch_card_v1", paths["db"],
                               paths["raw"], paths["pages"], paths["env"], ask, prices, make_client)
    return code, made, questions


def attempts(paths):
    conn = db.connect(paths["db"])
    try:
        return conn.execute("SELECT COUNT(*) FROM extraction").fetchone()[0]
    finally:
        conn.close()


def test_yes_runs_the_model_once_and_saves_the_attempt(world, capsys):
    add_deck(world)
    code, made, questions = run(world)
    assert code == 0 and len(made[0].calls) == 1 and attempts(world) == 1
    assert "2 pages" in questions[0] and "about $0.1" in questions[0]  # (2 x 1,900 + 2,500) x $2/M + $0.10
    assert "Real cost: $0.0100" in capsys.readouterr().out


def test_anything_but_yes_sends_nothing(world, capsys):
    add_deck(world)
    code, made, _ = run(world, answer="y")
    assert code == 3 and made == [] and attempts(world) == 0
    assert "Nothing was sent" in capsys.readouterr().out


def test_model_missing_from_the_price_list_is_refused(world):
    add_deck(world)
    code, made, questions = run(world, model="anthropic/claude-sonet-5.5")  # typo
    assert code == 3 and made == [] and questions == [] and attempts(world) == 0


def test_price_list_unreachable_sends_nothing(world):
    add_deck(world)

    def offline():
        raise PriceUnavailable("could not read OpenRouter's price list: offline")

    code, made, _ = run(world, prices=offline)
    assert code == 3 and made == [] and attempts(world) == 0


def test_model_and_temperature_can_be_set_per_run(world):
    add_deck(world)
    code, made, questions = run(world, model="google/gemini-4-argon", temperature="none")
    assert code == 0
    assert made[0].settings.model == "google/gemini-4-argon"
    assert made[0].settings.temperature is None
    assert "google/gemini-4-argon" in questions[0]


def test_unknown_deck_name_cannot_start(world, capsys):
    add_deck(world)
    code, made, _ = run(world, deck="Gridcure")
    assert code == 2 and made == []
    assert "No deck named" in capsys.readouterr().out


def test_same_deck_name_twice_is_refused_with_a_list(world, capsys):
    add_deck(world, "Cyclica.pdf")
    add_deck(world, "cyclica.pdf", deck=make_pdf(["Another copy"]))
    code, made, _ = run(world)
    assert code == 2 and made == []
    assert "matches several decks" in capsys.readouterr().out


def test_local_server_says_price_unknown_and_still_asks(world):
    world["env"].write_text("TRIAGE_BASE_URL=http://localhost:11434/v1\nTRIAGE_MODEL=local-model\n"
                            "TRIAGE_API_KEY=unused\n")
    add_deck(world)
    code, _, questions = run(world, prices=lambda: pytest.fail("no price lookup for a local server"))
    assert code == 0 and "price unknown" in questions[0]


def test_missing_settings_cannot_start(world, capsys):
    world["env"].write_text("TRIAGE_BASE_URL=https://openrouter.ai/api/v1\nTRIAGE_MODEL=m\n")
    add_deck(world)
    code, made, _ = run(world)
    assert code == 2 and made == []
    assert "TRIAGE_API_KEY" in capsys.readouterr().out


def test_bad_temperature_cannot_start(world):
    add_deck(world)
    code, made, _ = run(world, temperature="zero")
    assert code == 2 and made == []