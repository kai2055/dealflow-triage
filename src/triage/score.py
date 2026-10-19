"""Scoring: every bake-off card against the hand-checked answer sheet. Code only, no model, no cost.

Decisions 85-89: five outcomes per field (correct, wrong, missed, invented, correctly empty); short
fields match after tidying; free text and headquarters are judged by a person once and saved, unless
both texts are the same after tidying; lists pair only when certain and a person settles the rest;
every successful attempt is scored.
"""

import json
import os
import sqlite3
import tomllib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from triage import db
from triage.bakeoff import PlanError, load_plan
from triage.card import PitchCard
from triage.quotes import tidy

TIDY_FIELDS = ("company_name", "website", "stage", "round_size")  # matched by code (decision 85)
JUDGED_FIELDS = ("one_liner", "sector", "product", "ai_role", "headquarters")  # judged by a person (85, 88)
SINGLE_FIELDS = ("company_name", "website", "one_liner", "sector", "stage", "round_size",
                 "headquarters", "product", "ai_role")  # card order

# Words that appear in many traction claims, so sharing one proves nothing about which claim it is.
GENERIC_WORDS = {
    "annual", "billion", "booked", "client", "clients", "contract", "contracts", "customer", "customers",
    "growth", "intent", "letter", "letters", "margin", "margins", "million", "month", "months", "partner",
    "partners", "partnership", "partnerships", "patient", "patients", "pilot", "pilots", "pipeline",
    "profit", "revenue", "sales", "signed", "users", "with", "year", "years",
}


# --- Tidying -------------------------------------------------------------------------------------

def _letters_and_digits(text: str) -> str:
    """Only letters and digits, lower case: 'GridCure, Inc.' -> 'gridcureinc'."""
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _website_key(text: str) -> str:
    text = text.strip().casefold()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    return _letters_and_digits(text.rstrip("/"))


def _same_short(name: str, sheet, model) -> bool:
    if name == "website":
        return _website_key(sheet) == _website_key(model)
    if name == "stage":
        return sheet == model
    if name == "round_size":
        return (abs(sheet.amount - model.amount) < 0.5 and sheet.currency == model.currency
                and sheet.status == model.status)
    return _letters_and_digits(sheet) == _letters_and_digits(model)


def _shown(value) -> str:
    if value is None:
        return "(empty)"
    if hasattr(value, "amount"):  # a round size
        return f"{value.amount:,.0f} {value.currency or '(no currency)'} {value.status.value}"
    if isinstance(value, Enum):  # a stage
        return value.value
    return str(value)


def _bare(word: str) -> str:
    start, end = 0, len(word)
    while start < end and not word[start].isalnum():
        start += 1
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return word[start:end]


def _pairing_marks(text: str) -> set[str]:
    """What can tie two traction claims together for certain: numbers, and names (a word written
    with a capital that is not a generic traction word). '$12M CONTRACTS' -> {'12m'}."""
    marks = set()
    for raw in unicodedata.normalize("NFKC", text).split():
        word = _bare(raw)
        key = word.casefold()
        if not word:
            continue
        if any(c.isdigit() for c in word):
            marks.add(key)
        elif len(word) >= 4 and any(c.isupper() for c in word) and key not in GENERIC_WORDS:
            marks.add(key)
    return marks


# --- Hand judgments (decision 86: asked once, saved in a committed file) -------------------------

def _toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=True)  # a JSON string with ASCII escapes is a valid TOML basic string


class Judgments:
    """answers/<round>/judgments.toml: one [[judgment]] per call a person made."""

    VERDICTS = ("correct", "wrong", "invented", "valid_extra", "same")

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: list[dict] = []
        if self.path.exists():
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [dict(e) for e in data.get("judgment", [])]

    @staticmethod
    def _key(deck, name, sheet, model):
        return (deck, name, tidy(sheet), tidy(model))

    def find(self, deck: str, name: str, sheet: str, model: str) -> str | None:
        wanted = self._key(deck, name, sheet, model)
        for e in self.entries:
            if self._key(e["deck"], e["field"], e["sheet"], e["model"]) == wanted:
                return e["verdict"]
        return None

    def for_model_item(self, deck: str, name: str, model: str) -> list[dict]:
        return [e for e in self.entries
                if (e["deck"], e["field"], tidy(e["model"])) == (deck, name, tidy(model))]

    def add(self, deck: str, name: str, sheet: str, model: str, verdict: str) -> None:
        if verdict not in self.VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        self.entries.append({"deck": deck, "field": name, "sheet": sheet, "model": model, "verdict": verdict})
        self.save()  # saved after every answer, so stopping never loses a judgment (decision 88)

    def save(self) -> None:
        lines = ["# Hand judgments for scoring (decisions 85, 86, 88). One [[judgment]] per call.",
                 "# verdict: correct | wrong | invented | valid_extra | same", ""]
        for e in self.entries:
            lines.append("[[judgment]]")
            for k in ("deck", "field", "sheet", "model", "verdict"):
                lines.append(f"{k} = {_toml_string(e[k])}")
            lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)


# --- Scoring one card ----------------------------------------------------------------------------

@dataclass
class CardScore:
    fields: Counter = field(default_factory=Counter)  # correct, wrong, missed, invented, empty, unjudged
    team: Counter = field(default_factory=Counter)  # correct, missed, invented, valid_extra, unjudged, role_differs
    traction: Counter = field(default_factory=Counter)  # correct, wrong_kind, missed, invented, valid_extra, unjudged
    hand_judged: int = 0
    notes: list[str] = field(default_factory=list)  # every outcome other than correct / correctly empty


class Judge:
    """Looks up saved judgments and asks the person for new ones (unless asking was stopped)."""

    def __init__(self, judgments: Judgments, ask, out):
        self.judgments, self.ask, self.out = judgments, ask, out
        self.stopped = False

    def question(self, header: list[str], choices: dict[str, str]) -> str | None:
        """Returns the chosen key, or None for skip / stop. q stops asking for the rest of the run."""
        if self.stopped:
            return None
        for line in header:
            self.out(line)
        prompt = ", ".join(f"{k} = {v}" for k, v in choices.items()) + ", s = skip, q = stop judging: "
        while True:
            try:
                answer = self.ask(prompt).strip().lower()
            except EOFError:
                answer = "q"
            if answer in choices:
                return answer
            if answer == "s":
                return None
            if answer == "q":
                self.stopped = True
                return None


def _score_single(name, sheet_fact, model_fact, deck, model_name, judge, score):
    sheet, model = sheet_fact.value, model_fact.value
    if sheet is None and model is None:
        score.fields["empty"] += 1
        return
    if model is None:
        score.fields["missed"] += 1
        score.notes.append(f"missed: {name} = {_shown(sheet)}")
        return
    if sheet is None:
        score.fields["invented"] += 1
        score.notes.append(f"invented: {name} = {_shown(model)} (quote page {model_fact.evidence.page})")
        return
    if name in TIDY_FIELDS:
        outcome = "correct" if _same_short(name, sheet, model) else "wrong"
    elif _letters_and_digits(sheet) == _letters_and_digits(model):
        outcome = "correct"  # the same words: certain, so nobody is asked (decision 89)
    else:
        verdict = judge.judgments.find(deck, name, sheet, model)
        if verdict is None:
            answer = judge.question(
                [f"[{deck}] {name}", f"  sheet: {sheet}", f"  model: {model}  ({model_name})"],
                {"c": "correct", "w": "wrong"})
            if answer is not None:
                verdict = {"c": "correct", "w": "wrong"}[answer]
                judge.judgments.add(deck, name, sheet, model, verdict)
        if verdict is None:
            score.fields["unjudged"] += 1
            score.notes.append(f"unjudged: {name}: sheet {sheet!r} / model {model!r}")
            return
        score.hand_judged += 1
        outcome = verdict
    score.fields[outcome] += 1
    if outcome == "wrong":
        score.notes.append(f"wrong: {name}: sheet {_shown(sheet)!r} / model {_shown(model)!r}")


def _item_text(name: str, item) -> str:
    return item.name if name == "team" else f"{item.kind.value}: {item.text}"


def _pair_outcome(name: str, sheet_item, model_item, counts: Counter, notes: list[str]) -> None:
    if name == "team":
        counts["correct"] += 1
        if _letters_and_digits(sheet_item.role or "") != _letters_and_digits(model_item.role or ""):
            counts["role_differs"] += 1
            notes.append(f"role differs: {sheet_item.name}: sheet {sheet_item.role!r} / model {model_item.role!r}")
    elif sheet_item.kind == model_item.kind:
        counts["correct"] += 1
    else:
        counts["wrong_kind"] += 1
        notes.append(f"wrong kind: traction {sheet_item.text!r}: sheet {sheet_item.kind.value} / "
                     f"model {model_item.kind.value}")


def _auto_pairs(name: str, sheet_items: list, model_items: list) -> list[tuple[int, int]]:
    """Pairs only when certain: same tidied name (team) or same tidied text (traction), then for
    traction a number or name that exactly one item on each side shares."""
    pairs, used_s, used_m = [], set(), set()
    same = (lambda s, m: _letters_and_digits(s.name) == _letters_and_digits(m.name)) if name == "team" \
        else (lambda s, m: tidy(s.text) == tidy(m.text))
    for si, s in enumerate(sheet_items):
        for mi, m in enumerate(model_items):
            if mi not in used_m and same(s, m):
                pairs.append((si, mi)); used_s.add(si); used_m.add(mi)
                break
    if name == "traction":
        marks_s = {si: _pairing_marks(s.text) for si, s in enumerate(sheet_items) if si not in used_s}
        marks_m = {mi: _pairing_marks(m.text) for mi, m in enumerate(model_items) if mi not in used_m}
        for si, ms in marks_s.items():
            partners = [mi for mi, mm in marks_m.items() if ms & mm]
            if len(partners) == 1:
                mi = partners[0]
                back = [sj for sj, ss in marks_s.items() if ss & marks_m[mi]]
                if back == [si] and mi not in used_m:
                    pairs.append((si, mi)); used_s.add(si); used_m.add(mi)
    return pairs


def _score_list(name: str, sheet_items: list, model_items: list, deck: str, model_name: str,
                judge: Judge, counts: Counter, score: CardScore) -> None:
    pairs = _auto_pairs(name, sheet_items, model_items)
    for si, mi in pairs:
        _pair_outcome(name, sheet_items[si], model_items[mi], counts, score.notes)
    open_sheet = [si for si in range(len(sheet_items)) if si not in {p[0] for p in pairs}]
    open_model = [mi for mi in range(len(model_items)) if mi not in {p[1] for p in pairs}]

    for mi in open_model:
        item = model_items[mi]
        text = _item_text(name, item)
        verdict, paired_with = None, None
        for e in judge.judgments.for_model_item(deck, name, text):
            if e["verdict"] in ("invented", "valid_extra"):
                verdict = e["verdict"]
            else:  # same as a sheet item, if that item is still unpaired
                match = [si for si in open_sheet if tidy(_item_text(name, sheet_items[si])) == tidy(e["sheet"])]
                if match:
                    verdict, paired_with = "same", match[0]
            if verdict:
                break
        if verdict is None:
            choices = {"i": "invented", "v": "valid extra"}
            header = [f"[{deck}] {name}: a model item the code could not pair  ({model_name})",
                      f"  model: {text}  (quote page {item.evidence.page})"]
            if open_sheet:
                header.append("  unpaired sheet items:")
                for n, si in enumerate(open_sheet, start=1):
                    header.append(f"    {n}. {_item_text(name, sheet_items[si])}")
                    choices[str(n)] = f"same as {n}"
            answer = judge.question(header, choices)
            if answer in ("i", "v"):
                verdict = {"i": "invented", "v": "valid_extra"}[answer]
                judge.judgments.add(deck, name, "", text, verdict)
            elif answer is not None:
                paired_with = open_sheet[int(answer) - 1]
                verdict = "same"
                judge.judgments.add(deck, name, _item_text(name, sheet_items[paired_with]), text, "same")
        if verdict is None:
            counts["unjudged"] += 1
            score.notes.append(f"unjudged: {name} item {text!r}")
            continue
        score.hand_judged += 1
        if verdict == "same":
            open_sheet.remove(paired_with)
            _pair_outcome(name, sheet_items[paired_with], item, counts, score.notes)
        else:
            counts[verdict] += 1
            label = "invented" if verdict == "invented" else "valid extra"
            score.notes.append(f"{label}: {name} item {text!r} (quote page {item.evidence.page})")

    for si in open_sheet:
        counts["missed"] += 1
        score.notes.append(f"missed: {name} item {_item_text(name, sheet_items[si])!r}")


def score_card(sheet: PitchCard, card: PitchCard, deck: str, model_name: str, judge: Judge) -> CardScore:
    score = CardScore()
    for name in SINGLE_FIELDS:
        _score_single(name, getattr(sheet, name), getattr(card, name), deck, model_name, judge, score)
    _score_list("team", sheet.team, card.team, deck, model_name, judge, score.team, score)
    _score_list("traction", sheet.traction, card.traction, deck, model_name, judge, score.traction, score)
    return score


# --- Scoring a whole plan ------------------------------------------------------------------------

@dataclass
class ModelTotals:
    runs_ok: int = 0
    runs_failed: int = 0
    cost: float = 0.0
    fields: Counter = field(default_factory=Counter)
    team: Counter = field(default_factory=Counter)
    traction: Counter = field(default_factory=Counter)
    quotes: Counter = field(default_factory=Counter)
    modes: set = field(default_factory=set)  # strict and/or json (decision 94)
    hand_judged: int = 0

    def total(self, outcome: str) -> int:
        """One outcome across single fields and both lists (wrong includes a wrong traction kind)."""
        n = self.fields[outcome] + self.team[outcome] + self.traction[outcome]
        return n + self.traction["wrong_kind"] if outcome == "wrong" else n


def _attempts(conn: sqlite3.Connection, pitch_id: str, prompt: str, models: list[str]):
    marks = ", ".join("?" for _ in models)
    return conn.execute(
        f"""SELECT * FROM extraction WHERE purpose = 'bakeoff' AND pitch_id = ? AND prompt_version = ?
            AND requested_model IN ({marks}) ORDER BY id""",
        (pitch_id, prompt, *models)).fetchall()


def _report(plan_path: Path, prompt: str, totals: dict[str, ModelTotals], details: list[str],
            not_scored: list[str], unjudged: int, scored_at: str) -> str:
    lines = [f"# {plan_path.stem} scores", "",
             f"Plan `{plan_path.as_posix()}`, instructions `{prompt}`, scored {scored_at} UTC. "
             "Answer sheets drafted by Kimi, every field checked by hand (decision 80).", ""]
    if unjudged:
        lines += [f"**Not final: {unjudged} items still unjudged.** Run `triage score` again to finish.", ""]
    lines += ["| Model | Runs ok / failed | Invented | Wrong | Missed | Correct | Correctly empty | "
              "Valid extra | Unjudged | Hand-judged | Cost | Output mode |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, t in totals.items():
        lines.append(f"| {name} | {t.runs_ok} / {t.runs_failed} | {t.total('invented')} | {t.total('wrong')} | "
                     f"{t.total('missed')} | {t.total('correct')} | {t.fields['empty']} | "
                     f"{t.team['valid_extra'] + t.traction['valid_extra']} | {t.total('unjudged')} | "
                     f"{t.hand_judged} | ${t.cost:.4f} | {', '.join(sorted(t.modes)) or '-'} |")
    lines += ["", "Counts cover the 9 single fields plus every team member and traction claim. "
              "Wrong includes a traction claim filed under the wrong kind; a different team role is "
              "listed below but still counts the person as correct (decision 88). Output mode: strict means "
              "the provider enforced the card layout; json means the layout was only in the instructions and "
              "the card was checked in code afterwards (decision 93).", "",
              "| Model | Quotes verified | Not in text | Unverifiable | Bad |", "|---|---|---|---|---|"]
    for name, t in totals.items():
        q = t.quotes
        lines.append(f"| {name} | {q['verified']} | {q['not_in_text']} | {q['unverifiable']} | {q['bad']} |")
    if not_scored:
        lines += ["", "Not scored (no answer sheet): " + ", ".join(not_scored) + "."]
    lines += ["", "## Every outcome other than correct", ""] + (details or ["None."])
    return "\n".join(lines) + "\n"


def score_plan(conn: sqlite3.Connection, plan_path: Path, answers_root: Path = Path("answers"),
               reports_root: Path = Path("reports"), ask=input, out=print, now=None) -> int:
    """Exit codes: 0 scored and final, 1 scored but some items unjudged, 2 cannot start,
    3 nothing to score yet."""
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    answers_dir = Path(answers_root) / plan_path.stem
    if not answers_dir.is_dir():
        raise PlanError(f"no answer folder {answers_dir.as_posix()}")
    model_names = [m.name for m in plan.models]

    sheets, not_scored = {}, []
    for deck in plan.decks:
        path = answers_dir / f"{deck}.json"
        if not path.exists():
            not_scored.append(deck)
            continue
        try:
            sheets[deck] = PitchCard.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise PlanError(f"answer sheet {path.as_posix()} is not a valid card: {e}") from e

    judge = Judge(Judgments(answers_dir / "judgments.toml"), ask, out)
    totals = {name: ModelTotals() for name in model_names}
    details, scored_any = [], False
    for deck, sheet in sheets.items():
        matches = db.pitches_with_deck_named(conn, deck)
        if len(matches) != 1:
            raise PlanError(f"deck {deck!r} matches {len(matches)} pitches; it must match exactly one")
        for row in _attempts(conn, matches[0][0], plan.prompt, model_names):
            t = totals[row["requested_model"]]
            t.cost += row["cost"] or 0.0
            t.modes.add(row["output_mode"])
            if row["outcome"] != "extracted":
                t.runs_failed += 1
                details += [f"### {row['requested_model']} on {deck} (attempt {row['id']})", "",
                            f"- failed: {row['failure']}", ""]
                continue
            scored_any = True
            t.runs_ok += 1
            for key, column in (("verified", "quotes_verified"), ("not_in_text", "quotes_not_in_text"),
                                ("unverifiable", "quotes_unverifiable"), ("bad", "quotes_bad")):
                t.quotes[key] += row[column] or 0
            card = PitchCard.model_validate_json(row["card_json"])
            s = score_card(sheet, card, deck, row["requested_model"], judge)
            t.fields.update(s.fields); t.team.update(s.team); t.traction.update(s.traction)
            t.hand_judged += s.hand_judged
            details += [f"### {row['requested_model']} on {deck} (attempt {row['id']})", ""]
            details += [f"- {n}" for n in s.notes] or ["- all correct"]
            details.append("")

    if not scored_any and not any(t.runs_failed for t in totals.values()):
        out("Nothing to score yet: no bake-off attempts for this plan.")
        return 3
    unjudged = sum(t.total("unjudged") for t in totals.values())
    scored_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    report = _report(plan_path, plan.prompt, totals, details, not_scored, unjudged, scored_at)
    report_path = Path(reports_root) / f"{plan_path.stem}_scores.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8", newline="\n")

    out("")
    out(f"{'Model':<34} {'ok/failed':>9} {'invented':>8} {'wrong':>5} {'missed':>6} {'correct':>7} {'unjudged':>8}")
    for name, t in totals.items():
        out(f"{name:<34} {f'{t.runs_ok}/{t.runs_failed}':>9} {t.total('invented'):>8} {t.total('wrong'):>5} "
            f"{t.total('missed'):>6} {t.total('correct'):>7} {t.total('unjudged'):>8}")
    if not_scored:
        out(f"Not scored (no answer sheet): {', '.join(not_scored)}")
    out(f"Report: {report_path.as_posix()}")
    if unjudged:
        out(f"{unjudged} items still unjudged: run the same command again to finish.")
        return 1
    return 0