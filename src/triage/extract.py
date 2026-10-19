"""The extraction step: one pitch in, one saved attempt out.

deck -> pages -> model -> card check -> quote check -> saved attempt (and, for pipeline runs, the pitch's state).
Every outcome is saved, failures included (decisions 55, 63, 64).
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from triage import db
from triage.card import PitchCard
from triage.model import CallRecord, ModelCallError, ModelClient, ModelSettings
from triage.models import PitchState
from triage.pages import DEFAULT_PAGES_DIR, DeckError, deck_to_pages
from triage.quotes import QuoteReport, QuoteStatus, check_quotes
from triage.storage import RawStore

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
DEFAULT_PROMPT = "pitch_card_v2"  # v2: whole facts are null when empty (decision 92)
PURPOSES = ("pipeline", "bakeoff")

SINGLE_FACTS = ("company_name", "website", "one_liner", "sector", "stage", "round_size",
                "headquarters", "product", "ai_role")

# Rules strict mode can't express; PitchCard still enforces every one of them in code (decision 65).
_NOT_FOR_STRICT_MODE = {"title", "default", "minLength", "maxLength", "minimum", "maximum",
                        "exclusiveMinimum", "exclusiveMaximum", "pattern", "format", "minItems", "maxItems"}


def _not_null(node: dict) -> dict:
    """The branch of 'this or null' that is not null."""
    return next(branch for branch in node["anyOf"] if branch.get("type") != "null")


def strict_layout() -> dict:
    """The card's layout, simplified for strict mode: every field required (empty = null), no rules,
    no cross-references. Derived from PitchCard, so the two can never disagree (decision 65).
    A single fact is null as a whole, or a value with its evidence, both filled (decision 92)."""
    schema = PitchCard.model_json_schema()
    parts = schema.pop("$defs", {})

    def simplify(node):
        if isinstance(node, list):
            return [simplify(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:  # write the referenced part out in place
            return simplify(parts[node["$ref"].rsplit("/", 1)[-1]])
        out = {key: simplify(value) for key, value in node.items() if key not in _NOT_FOR_STRICT_MODE}
        if "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    layout = simplify(schema)
    for name in SINGLE_FACTS:
        fact = layout["properties"][name]
        filled = {"type": "object", "properties": {"value": _not_null(fact["properties"]["value"]),
                                                   "evidence": _not_null(fact["properties"]["evidence"])},
                  "required": ["value", "evidence"], "additionalProperties": False}
        layout["properties"][name] = {"anyOf": [filled, {"type": "null"}]}
    return layout


def count_emptyable(node) -> int:
    """How many fields in a layout may be null or one of several types (finding 16)."""
    if isinstance(node, list):
        return sum(count_emptyable(item) for item in node)
    if not isinstance(node, dict):
        return 0
    own = 1 if isinstance(node.get("type"), list) or "anyOf" in node else 0
    return own + sum(count_emptyable(value) for value in node.values())


def card_from_answer(raw_text: str) -> PitchCard:
    """The model's answer as a PitchCard: a null fact becomes an empty Fact (decision 92).
    Raises ValueError (ValidationError included) when the answer is not a valid card."""
    data = json.loads(raw_text)
    if isinstance(data, dict):
        for name in SINGLE_FACTS:
            if name in data and data[name] is None:
                data[name] = {"value": None, "evidence": None}
    return PitchCard.model_validate(data)


def load_instructions(version: str = DEFAULT_PROMPT, folder: Path = PROMPTS_DIR) -> str:
    """The model's instructions, by version name (decision 66)."""
    return (Path(folder) / f"{version}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Attempt:
    id: int
    outcome: str  # "extracted" or "failed"
    failure: str | None  # e.g. "deck_locked", "model_unavailable", "invalid_card"
    card: PitchCard | None
    report: QuoteReport | None


def extract_pitch(
    conn: sqlite3.Connection,
    store: RawStore,
    client: ModelClient,
    settings: ModelSettings,
    pitch_id: str,
    purpose: str = "pipeline",
    prompt_version: str = DEFAULT_PROMPT,
    pages_root: Path = DEFAULT_PAGES_DIR,
) -> Attempt:
    """Read one pitch's deck with the model and save the attempt. Only pipeline runs move the
    pitch's state; bake-off runs are saved and counted but never change it (decision 64)."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be pipeline or bakeoff, not {purpose!r}")
    instructions = load_instructions(prompt_version)  # a missing version stops before anything is saved
    row = {
        "pitch_id": pitch_id, "purpose": purpose,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt_version": prompt_version, "requested_model": settings.model,
        "output_mode": settings.output_mode, "temperature": settings.temperature,
        "data_collection": settings.data_collection,
    }
    card = report = None
    failure = detail = None
    record: CallRecord | None = None
    raw_text = None

    decks = db.decks_of_pitch(conn, pitch_id)
    if len(decks) != 1:
        failure = "no_deck" if not decks else "several_decks"
        detail = f"{len(decks)} readable PDF decks attached"
    else:
        try:
            pages = deck_to_pages(store, decks[0], pages_root).pages
            result = client.read(instructions, pages, "pitch_card", strict_layout())
            record, raw_text = result.record, result.raw_text
            card = card_from_answer(raw_text)  # the code decides (decision 55)
            report = check_quotes(card, pages)  # the card itself is never changed (decision 61)
        except DeckError as e:
            failure, detail = f"deck_{e.reason}", e.detail
        except ModelCallError as e:
            failure, detail = f"model_{e.kind}", e.detail
        except ValueError as e:  # not JSON, or JSON that is not a valid card (ValidationError is a ValueError)
            failure, detail = "invalid_card", str(e)

    if record:
        row.update(answered_model=record.answered_model, provider=record.provider,
                   input_tokens=record.input_tokens, output_tokens=record.output_tokens, cost=record.cost)
    if card and report:
        row.update(
            completeness=card.completeness(), card_json=card.model_dump_json(),
            quote_report_json=json.dumps([asdict(c) for c in report.checks]),
            quotes_verified=report.count(QuoteStatus.VERIFIED),
            quotes_not_in_text=report.count(QuoteStatus.NOT_IN_TEXT),
            quotes_bad=report.count(QuoteStatus.BAD_QUOTE),
            quotes_unverifiable=report.count(QuoteStatus.UNVERIFIABLE),
        )
    outcome = "failed" if failure else "extracted"
    row.update(outcome=outcome, failure=failure, failure_detail=detail, raw_text=raw_text)
    attempt_id = db.insert_extraction(conn, row)

    if purpose == "pipeline":
        state = PitchState.FAILED if failure else PitchState.EXTRACTED
        db.set_state(conn, pitch_id, state, f"extraction {attempt_id}: {failure or settings.model}")
    return Attempt(attempt_id, outcome, failure, card, report)