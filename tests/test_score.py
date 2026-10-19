"""Tests for scoring: cards against answer sheets, hand judgments asked once, lists paired only when certain."""

import tomllib
from datetime import datetime, timezone

import pytest

from triage import db
from triage.bakeoff import PlanError
from triage.card import Claim, ClaimKind, Evidence, Fact, Person, PitchCard, RoundSize, RoundStatus, Stage
from triage.cli import score_command
from triage.models import Attachment, RawPitch
from triage.score import score_plan

NOW = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)
PLAN_TEXT = """
prompt = "pitch_card_v1"
decks = ["Gridcure", "Cyclica"]

[[model]]
name = "a/strong"
temperature = "none"

[[model]]
name = "b/strong"
temperature = "0"
"""


def ev(page=1, quote="q"):
    return Evidence(quote=quote, page=page)


def fact(value, page=1):
    return Fact(value=value, evidence=ev(page))


def claim(kind, text, page=4):
    return Claim(kind=ClaimKind(kind), text=text, evidence=ev(page))


def person(name, role=None):
    return Person(name=name, role=role, evidence=ev(6))


GRIDCURE_SHEET = PitchCard(
    company_name=fact("GridCure"), website=fact("angel.co/gridcure"),
    one_liner=fact("Powerful analytics for grid security"),
    team=[person("Tagg Jefferson"), person("Kim Montgomery", "Ph. D Applied Mathematics")],
    traction=[claim("contracts", "$12M CONTRACTS"), claim("partnership", "BURNS McDONNELL"),
              claim("pipeline", "$105M PIPELINE")],
)


class World:
    def __init__(self, tmp_path):
        self.conn = db.connect(tmp_path / "test.db")
        self.root = tmp_path
        self.pitch = {}
        for i, deck in enumerate(["Gridcure", "Cyclica"]):
            sha = f"{i + 1}" * 64
            pitch_id, _ = db.insert_pitch(self.conn, RawPitch(
                source="folder", raw_hash=sha, received_at=NOW, attachments=[Attachment(
                    sha256=sha, filename=f"{deck}.pdf", detected_type="pdf", size_bytes=10, supported=True)]))
            self.pitch[deck] = pitch_id
        self.plan = tmp_path / "bakeoff" / "round1.toml"
        self.plan.parent.mkdir()
        self.plan.write_text(PLAN_TEXT)
        self.answers = tmp_path / "answers" / "round1"
        self.answers.mkdir(parents=True)
        self.reports = tmp_path / "reports"

    def sheet(self, deck, card):
        (self.answers / f"{deck}.json").write_text(card.model_dump_json(indent=2), encoding="utf-8")

    def attempt(self, deck, model, card=None, failure=None, cost=0.01, quotes=(3, 1, 2, 0)):
        row = dict(pitch_id=self.pitch[deck], purpose="bakeoff", started_at=NOW.isoformat(),
                   outcome="failed" if failure else "extracted", failure=failure,
                   prompt_version="pitch_card_v1", requested_model=model, output_mode="strict",
                   data_collection="deny", cost=cost)
        if card is not None:
            row.update(card_json=card.model_dump_json(), quotes_verified=quotes[0], quotes_not_in_text=quotes[1],
                       quotes_unverifiable=quotes[2], quotes_bad=quotes[3])
        return db.insert_extraction(self.conn, row)

    def score(self, answers=(), **kwargs):
        replies = list(answers)
        asked, printed = [], []

        def ask(prompt):
            asked.append(prompt)
            if not replies:
                raise AssertionError(f"asked more than expected: {prompt}")
            return replies.pop(0)

        code = score_plan(self.conn, self.plan, self.root / "answers", self.reports, ask, printed.append,
                          now=NOW, **kwargs)
        self.asked, self.printed = asked, printed
        return code

    @property
    def report(self):
        return (self.reports / "round1_scores.md").read_text(encoding="utf-8")

    def judgments(self):
        return tomllib.loads((self.answers / "judgments.toml").read_text(encoding="utf-8"))["judgment"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def row_for(world, model):
    line = next(l for l in world.report.splitlines() if l.startswith(f"| {model} |") and "/" in l)
    return [cell.strip() for cell in line.strip("|").split("|")]


# --- Single fields --------------------------------------------------------------------------

def test_short_fields_match_after_tidying_and_a_different_value_is_wrong(world):
    world.sheet("Gridcure", PitchCard(company_name=fact("GridCure"), website=fact("angel.co/gridcure"),
                                      round_size=fact(RoundSize(amount=20e6, currency="CAD", status=RoundStatus.SOUGHT))))
    world.attempt("Gridcure", "a/strong", PitchCard(
        company_name=fact("Gridcure"), website=fact("https://www.Angel.co/gridcure/"),
        round_size=fact(RoundSize(amount=20e6, currency=None, status=RoundStatus.SOUGHT))))
    assert world.score() == 0
    cells = row_for(world, "a/strong")
    assert (cells[3], cells[5]) == ("1", "2")  # wrong 1 (currency left empty), correct 2
    assert "wrong: round_size" in world.report


def test_missed_invented_and_correctly_empty_are_counted_by_code(world):
    world.sheet("Gridcure", PitchCard(company_name=fact("GridCure"), stage=fact(Stage.SEED)))
    world.attempt("Gridcure", "a/strong", PitchCard(company_name=fact("GridCure"), website=fact("gridcure.com")))
    assert world.score() == 0 and world.asked == []
    cells = row_for(world, "a/strong")
    assert (cells[2], cells[4], cells[5], cells[6]) == ("1", "1", "1", "6")  # invented, missed, correct, empty
    assert "invented: website = gridcure.com" in world.report and "missed: stage = seed" in world.report


# --- Hand judgments (decisions 85, 86, 88) ----------------------------------------------------

def test_free_text_is_asked_once_saved_and_reused(world):
    world.sheet("Gridcure", PitchCard(one_liner=fact("Powerful analytics for grid security")))
    world.attempt("Gridcure", "a/strong", PitchCard(one_liner=fact("Analytics for grid security")))
    assert world.score(["c"]) == 0 and len(world.asked) == 1
    [j] = world.judgments()
    assert (j["field"], j["model"], j["verdict"]) == ("one_liner", "Analytics for grid security", "correct")
    assert world.score([]) == 0  # second run: nothing asked
    assert row_for(world, "a/strong")[9] == "1"  # hand-judged


def test_the_same_words_are_correct_without_asking(world):
    world.sheet("Gridcure", PitchCard(one_liner=fact("Powerful analytics for grid security"),
                                      product=fact("Your medical bill")))
    world.attempt("Gridcure", "a/strong", PitchCard(one_liner=fact("POWERFUL ANALYTICS, for grid security."),
                                                    product=fact("Your medical bill")))
    assert world.score() == 0 and world.asked == []  # decision 89
    assert row_for(world, "a/strong")[5] == "2" and row_for(world, "a/strong")[9] == "0"
    assert not (world.answers / "judgments.toml").exists()


def test_identical_text_from_two_models_is_judged_once(world):
    world.sheet("Gridcure", PitchCard(headquarters=fact("207 Queens Quay West, Toronto, Ontario M5J 1A7")))
    for model in ("a/strong", "b/strong"):
        world.attempt("Gridcure", model, PitchCard(headquarters=fact("Toronto, Ontario")))
    assert world.score(["c"]) == 0 and len(world.asked) == 1  # headquarters is judged (decision 88)
    assert row_for(world, "b/strong")[5] == "1"


def test_skip_leaves_items_unjudged_until_a_later_run_finishes_them(world):
    world.sheet("Gridcure", PitchCard(sector=fact("utilities"), product=fact("analytics")))
    world.attempt("Gridcure", "a/strong", PitchCard(sector=fact("energy"), product=fact("grid analytics")))
    assert world.score(["s", "w"]) == 1
    assert "Not final: 1 items still unjudged" in world.report
    assert world.score(["c"]) == 0 and len(world.asked) == 1  # only the skipped one is asked again
    assert "Not final" not in world.report


def test_q_stops_asking_for_the_rest_of_the_run(world):
    world.sheet("Gridcure", PitchCard(sector=fact("utilities"), product=fact("analytics")))
    world.attempt("Gridcure", "a/strong", PitchCard(sector=fact("energy"), product=fact("grid analytics")))
    assert world.score(["q"]) == 1 and len(world.asked) == 1
    assert row_for(world, "a/strong")[8] == "2"


def test_judgments_file_keeps_quotes_backslashes_and_accents(world):
    tricky = 'Tie\u00f6s "AI-first" C:\\path'
    world.sheet("Gridcure", PitchCard(product=fact("x")))
    world.attempt("Gridcure", "a/strong", PitchCard(product=fact(tricky)))
    world.score(["w"])
    assert world.judgments()[0]["model"] == tricky
    assert (world.answers / "judgments.toml").read_bytes().isascii()


# --- Lists ------------------------------------------------------------------------------------

def test_team_pairs_by_name_and_a_different_role_is_listed_but_still_correct(world):
    world.sheet("Gridcure", GRIDCURE_SHEET.model_copy(update={"traction": [], "one_liner": Fact()}))
    world.attempt("Gridcure", "a/strong", PitchCard(
        company_name=fact("GridCure"), website=fact("angel.co/gridcure"),
        team=[person("TAGG JEFFERSON", "CEO"), person("Kim Montgomery", "Ph.D. Applied Mathematics")]))
    assert world.score() == 0 and world.asked == []
    assert row_for(world, "a/strong")[5] == "4"  # 2 fields + 2 people
    assert "role differs: Tagg Jefferson: sheet None / model 'CEO'" in world.report
    assert "role differs: Kim" not in world.report  # punctuation and spaces are layout


def test_traction_pairs_by_a_shared_number_and_a_different_kind_is_wrong(world):
    world.sheet("Gridcure", PitchCard(traction=[claim("contracts", "$12M CONTRACTS"),
                                                claim("pipeline", "$105M PIPELINE")]))
    world.attempt("Gridcure", "a/strong", PitchCard(traction=[
        claim("contracts", "Signed contracts worth $12M"), claim("revenue", "$105M pipeline of deals")]))
    assert world.score() == 0 and world.asked == []
    cells = row_for(world, "a/strong")
    assert (cells[3], cells[5]) == ("1", "1")
    assert "wrong kind: traction '$105M PIPELINE': sheet pipeline / model revenue" in world.report


def test_a_number_shared_by_two_items_is_not_certain_so_a_person_decides(world):
    world.sheet("Gridcure", PitchCard(traction=[claim("other", "Founded 2014"), claim("other", "Patent filed 2014")]))
    world.attempt("Gridcure", "a/strong", PitchCard(traction=[claim("other", "Operating since 2014")]))
    assert world.score(["1"]) == 0
    assert "1. other: Founded 2014" in "\n".join(world.printed)
    assert "missed: traction item 'other: Patent filed 2014'" in world.report


def test_a_shared_name_pairs_two_claims_without_asking(world):
    world.sheet("Gridcure", PitchCard(traction=[claim("partnership", "BURNS McDONNELL")]))
    world.attempt("Gridcure", "a/strong", PitchCard(traction=[claim("partnership", "Burns & McDonnell")]))
    assert world.score() == 0 and world.asked == []
    assert row_for(world, "a/strong")[5] == "1"


def test_unpaired_model_items_are_marked_invented_valid_extra_or_same(world):
    world.sheet("Gridcure", PitchCard(traction=[claim("partnership", "BURNS McDONNELL")]))
    world.attempt("Gridcure", "a/strong", PitchCard(traction=[
        claim("partnership", "a large engineering firm"), claim("customers", "3 utilities"), claim("revenue", "$5M revenue")]))
    assert world.score(["1", "v", "i"]) == 0
    cells = row_for(world, "a/strong")
    assert (cells[2], cells[5], cells[7], cells[9]) == ("1", "1", "1", "3")  # invented, correct, valid extra, hand
    assert {j["verdict"] for j in world.judgments()} == {"same", "valid_extra", "invented"}
    assert world.score([]) == 0  # every call reused


def test_sheet_items_the_model_left_out_are_missed(world):
    world.sheet("Gridcure", GRIDCURE_SHEET.model_copy(update={"one_liner": Fact()}))
    world.attempt("Gridcure", "a/strong", PitchCard(company_name=fact("GridCure"), website=fact("angel.co/gridcure")))
    assert world.score() == 0
    assert row_for(world, "a/strong")[4] == "5"  # 2 people + 3 claims


# --- The whole plan ---------------------------------------------------------------------------

def test_deck_without_a_sheet_is_listed_as_not_scored(world):
    world.sheet("Gridcure", PitchCard(company_name=fact("GridCure")))
    world.attempt("Gridcure", "a/strong", PitchCard(company_name=fact("GridCure")))
    world.attempt("Cyclica", "a/strong", PitchCard(company_name=fact("Cyclica")))
    assert world.score() == 0
    assert "Not scored (no answer sheet): Cyclica." in world.report
    assert row_for(world, "a/strong")[1] == "1 / 0"


def test_failed_attempts_count_as_failures_and_every_success_is_scored(world):
    world.sheet("Gridcure", PitchCard(company_name=fact("GridCure")))
    world.attempt("Gridcure", "a/strong", PitchCard(company_name=fact("GridCure")), quotes=(1, 0, 0, 0))
    world.attempt("Gridcure", "a/strong", PitchCard(company_name=fact("Grid Cure Inc")), quotes=(0, 1, 0, 0))
    world.attempt("Gridcure", "b/strong", failure="model_unavailable", cost=None)
    assert world.score() == 0
    assert row_for(world, "a/strong")[1:4] == ["2 / 0", "0", "1"]  # the repeat run is scored too
    assert row_for(world, "b/strong")[1] == "0 / 1"
    assert "| a/strong | 1 | 1 | 0 | 0 |" in world.report  # quote counts summed
    assert "- failed: model_unavailable" in world.report


def test_nothing_to_score_and_missing_answer_folder(world, tmp_path):
    world.sheet("Gridcure", PitchCard())
    assert world.score() == 3
    (tmp_path / "bakeoff" / "round2.toml").write_text(PLAN_TEXT)
    with pytest.raises(PlanError, match="no answer folder"):
        score_plan(world.conn, tmp_path / "bakeoff" / "round2.toml", tmp_path / "answers", world.reports)


def test_score_command_reports_a_bad_answer_sheet_without_crashing(world, tmp_path, capsys):
    (world.answers / "Gridcure.json").write_text('{"company_name": {"value": "GridCure"}}')
    world.conn.close()
    code = score_command(world.plan, tmp_path / "test.db", tmp_path / "answers", world.reports)
    world.conn = db.connect(tmp_path / "test.db")
    assert code == 2 and "not a valid card" in capsys.readouterr().out