"""The bake-off: every model in a plan file on every deck, as bakeoff attempts, after one "yes".

Decisions 64, 74, 77-79: bake-off runs never move a pitch's state; one yes for the whole plan with a
hard stop at the agreed worst case; combinations that already succeeded are skipped unless --again.
"""

import dataclasses
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from triage import cost, db
from triage.extract import DEFAULT_PROMPT, extract_pitch
from triage.model import ModelSettings, OpenAICompatibleClient
from triage.pages import DEFAULT_PAGES_DIR, DeckError, deck_to_pages
from triage.storage import RawStore


class PlanError(Exception):
    """The plan file is wrong, or names a deck that can't be found. Nothing was sent."""


@dataclass(frozen=True)
class PlanModel:
    name: str
    temperature: float | None  # None: send no temperature (decision 73)


@dataclass(frozen=True)
class Plan:
    prompt: str
    decks: tuple[str, ...]
    models: tuple[PlanModel, ...]


def load_plan(path: Path) -> Plan:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise PlanError(f"can't read plan {path}: {e}") from e
    decks = data.get("decks") or []
    entries = data.get("model") or []
    if not decks or not entries:
        raise PlanError("a plan needs a decks list and at least one [[model]]")
    models = []
    for entry in entries:
        if "name" not in entry:
            raise PlanError("every [[model]] needs a name")
        text = str(entry.get("temperature", "none")).strip().lower()
        try:
            temperature = None if text == "none" else float(text)
        except ValueError:
            raise PlanError(f"temperature for {entry['name']} must be a number or none, not {text!r}")
        models.append(PlanModel(entry["name"], temperature))
    return Plan(data.get("prompt", DEFAULT_PROMPT), tuple(decks), tuple(models))


def _already_done(conn: sqlite3.Connection, pitch_id: str, model: str, prompt: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM extraction WHERE pitch_id = ? AND requested_model = ? AND prompt_version = ?
           AND purpose = 'bakeoff' AND outcome = 'extracted' LIMIT 1""",
        (pitch_id, model, prompt),
    ).fetchone() is not None


def run_plan(conn: sqlite3.Connection, store: RawStore, plan: Plan, base: ModelSettings,
             pages_dir: Path = DEFAULT_PAGES_DIR, again: bool = False, ask=input,
             prices=cost.fetch_prices, make_client=OpenAICompatibleClient, out=print) -> int:
    """Exit codes: 0 all runs done, 1 some attempts failed, 3 nothing sent, 4 stopped at the spending limit."""
    pitch_of = {}
    for deck in plan.decks:  # every deck must resolve before anything is priced or sent
        matches = db.pitches_with_deck_named(conn, deck)
        if len(matches) != 1:
            raise PlanError(f"deck {deck!r} matches {len(matches)} pitches; it must match exactly one")
        pitch_of[deck] = matches[0][0]

    page_counts = {}
    for deck, pitch_id in pitch_of.items():  # free: rendering only
        try:
            page_counts[deck] = len(deck_to_pages(store, db.decks_of_pitch(conn, pitch_id)[0], pages_dir).pages)
        except DeckError:
            page_counts[deck] = None  # saved as a failed attempt, no model call, no cost

    priced = cost.is_openrouter(base.base_url)
    price_list = {}
    if priced:
        try:
            listing = prices()
            price_list = {m.name: cost.price_for(m.name, listing) for m in plan.models}
        except cost.PriceUnavailable as e:
            out(f"Nothing sent: {e}")
            return 3

    runs, skipped = [], 0
    for m in plan.models:
        for deck in plan.decks:
            if not again and _already_done(conn, pitch_of[deck], m.name, plan.prompt):
                skipped += 1
                continue
            pages = page_counts[deck]
            guess = cost.estimate(pages, price_list[m.name], base.max_retries) if priced and pages else None
            runs.append((m, deck, guess))

    if not runs:
        out(f"Nothing to run: all {skipped} combinations already succeeded (use --again to repeat).")
        return 0
    for m, deck, guess in runs:
        shown = f"${guess.expected:.3f}" if guess else ("no model call" if page_counts[deck] is None else "price unknown")
        out(f"  {m.name:<34} {deck:<18} {page_counts[deck] or '-':>3} pages  {shown}")
    if skipped:
        out(f"  ({skipped} combinations already succeeded, skipped)")
    if priced:
        expected = sum(g.expected for _, _, g in runs if g)
        limit = sum(g.worst_case for _, _, g in runs if g)
        question = f"{len(runs)} runs: about ${expected:.2f}, stops if real spending passes ${limit:.2f}. Type yes to spend: "
    else:
        limit = None
        question = f"{len(runs)} runs on {base.base_url}: price unknown (not OpenRouter). Type yes to run: "
    if not cost.confirmed(question, ask):
        out("Cancelled. Nothing was sent.")
        return 3

    spent, failed = 0.0, 0
    for i, (m, deck, guess) in enumerate(runs):
        if limit is not None and spent > limit:
            out(f"Stopped: ${spent:.4f} spent passes the agreed ${limit:.2f}. {len(runs) - i} runs not started.")
            return 4
        settings = dataclasses.replace(base, model=m.name, temperature=m.temperature)
        attempt = extract_pitch(conn, store, make_client(settings), settings, pitch_of[deck], "bakeoff",
                                plan.prompt, pages_dir)
        row = conn.execute("SELECT * FROM extraction WHERE id = ?", (attempt.id,)).fetchone()
        if row["cost"] is not None:
            spent += row["cost"]
        elif guess and row["answered_model"]:  # a call happened but no cost was reported: count the worst case
            spent += guess.worst_case
        if attempt.outcome == "extracted":
            result = (f"extracted, {row['quotes_verified']} verified / {row['quotes_not_in_text']} not in text / "
                      f"{row['quotes_unverifiable']} unverifiable / {row['quotes_bad']} bad")
        else:
            failed += 1
            result = f"failed ({attempt.failure})"
        out(f"  [{i + 1}/{len(runs)}] {m.name} on {deck}: {result}. Running total ${spent:.4f}")
    out(f"Done: {len(runs) - failed} extracted, {failed} failed, {skipped} skipped. Spent ${spent:.4f}.")
    return 1 if failed else 0