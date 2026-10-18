"""Command line: `triage ingest --folder data/inbox` and `triage extract --deck Cyclica`."""

import argparse
import dataclasses
import sqlite3
import sys
from pathlib import Path

from triage import cost, db
from triage.extract import DEFAULT_PROMPT, extract_pitch
from triage.intake.folder import list_files, read_file
from triage.model import OpenAICompatibleClient, SettingsError, load_settings
from triage.models import PitchState
from triage.pages import DEFAULT_PAGES_DIR, DeckError, deck_to_pages
from triage.storage import DEFAULT_RAW_DIR, RawStore


def ingest(folder: Path, store: RawStore, conn: sqlite3.Connection) -> dict[str, int]:
    """Ingest every file in a folder; one broken file never stops the others."""
    counts = {"new": 0, "possible duplicate": 0, "duplicate": 0, "failed": 0, "error": 0}
    for path in list_files(folder):
        try:
            item = read_file(path, store)
            pitch_id, inserted = db.insert_pitch(conn, item.pitch, reason=item.note or "ingested")
            if not inserted:
                outcome = "duplicate"
            elif item.pitch.state == PitchState.FAILED:
                outcome = "failed"
            else:
                outcome = "new"
                for deck in item.pitch.attachments:
                    if not deck.supported:
                        continue
                    earlier = db.find_pitch_with_deck(conn, deck.sha256, pitch_id)
                    if earlier:
                        db.mark_possible_duplicate(conn, pitch_id, earlier)
                        outcome = "possible duplicate"
                        break
        except Exception as exc:
            counts["error"] += 1
            print(f"{path.name:<32} {'ERROR':<20} {type(exc).__name__}: {exc}")
            continue
        counts[outcome] += 1
        print(f"{path.name:<32} {outcome:<20} {pitch_id[:8]}")
    return counts


def extract_command(deck: str, model: str | None, temperature: str | None, purpose: str,
                    prompt: str, db_path: Path, raw_dir: Path, pages_dir: Path, env_file: Path,
                    ask=input, prices=cost.fetch_prices, make_client=OpenAICompatibleClient) -> int:
    """Read one deck with one model. Exit codes: 0 extracted, 1 attempt failed, 2 cannot start,
    3 nothing sent (no price, or not confirmed)."""
    try:
        settings = load_settings(env_file)
    except SettingsError as e:
        print(f"Settings: {e}")
        return 2
    if model:
        settings = dataclasses.replace(settings, model=model)
    if temperature is not None:
        try:
            value = None if temperature.lower() == "none" else float(temperature)
        except ValueError:
            print(f"--temperature must be a number or none, not {temperature!r}")
            return 2
        settings = dataclasses.replace(settings, temperature=value)

    conn = db.connect(db_path)
    try:
        matches = db.pitches_with_deck_named(conn, deck)
        if len(matches) != 1:
            print(f"No deck named {deck!r}." if not matches else f"{deck!r} matches several decks:")
            for pitch_id, filename in matches:
                print(f"  {pitch_id[:8]}  {filename}")
            return 2
        pitch_id = matches[0][0]
        store = RawStore(raw_dir)

        try:  # free: rendering only; a broken deck is saved as a failed attempt without any model call
            page_count = len(deck_to_pages(store, db.decks_of_pitch(conn, pitch_id)[0], pages_dir).pages)
        except DeckError:
            page_count = None

        if page_count is not None:
            if cost.is_openrouter(settings.base_url):
                try:
                    price = cost.price_for(settings.model, prices())
                except cost.PriceUnavailable as e:
                    print(f"Nothing sent: {e}")
                    return 3
                guess = cost.estimate(page_count, price, settings.max_retries)
                question = (f"{deck} ({page_count} pages) with {settings.model}: about ${guess.expected:.3f} "
                            f"(worst case ${guess.worst_case:.3f}). Type yes to spend: ")
            else:
                question = (f"{deck} ({page_count} pages) with {settings.model} on {settings.base_url}: "
                            f"price unknown (not OpenRouter). Type yes to run: ")
            if not cost.confirmed(question, ask):
                print("Cancelled. Nothing was sent.")
                return 3

        attempt = extract_pitch(conn, store, make_client(settings), settings, pitch_id, purpose,
                                prompt, pages_dir)
        row = conn.execute("SELECT * FROM extraction WHERE id = ?", (attempt.id,)).fetchone()
        if attempt.outcome == "extracted":
            print(f"Attempt {attempt.id}: extracted. Completeness {row['completeness']:.0%}. Quotes: "
                  f"{row['quotes_verified']} verified, {row['quotes_bad']} bad, "
                  f"{row['quotes_unverifiable']} unverifiable.")
        else:
            print(f"Attempt {attempt.id}: failed ({attempt.failure}): {row['failure_detail']}")
        spent = f"${row['cost']:.4f}" if row["cost"] is not None else "not reported"
        print(f"Real cost: {spent}. Tokens in/out: {row['input_tokens']}/{row['output_tokens']}.")
        return 0 if attempt.outcome == "extracted" else 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage", description="Dealflow triage prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_cmd = commands.add_parser("ingest", help="Ingest every file in a folder")
    ingest_cmd.add_argument("--folder", type=Path, required=True)
    ingest_cmd.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    ingest_cmd.add_argument("--raw", type=Path, default=DEFAULT_RAW_DIR)

    extract_cmd = commands.add_parser("extract", help="Read one deck with one model (asks before spending)")
    extract_cmd.add_argument("--deck", required=True, help="deck file name, e.g. Cyclica")
    extract_cmd.add_argument("--model", help="overrides TRIAGE_MODEL for this run")
    extract_cmd.add_argument("--temperature", help="overrides TRIAGE_TEMPERATURE: a number, or none")
    extract_cmd.add_argument("--purpose", choices=["pipeline", "bakeoff"], default="pipeline")
    extract_cmd.add_argument("--prompt", default=DEFAULT_PROMPT)
    extract_cmd.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    extract_cmd.add_argument("--raw", type=Path, default=DEFAULT_RAW_DIR)
    extract_cmd.add_argument("--pages", type=Path, default=DEFAULT_PAGES_DIR)
    extract_cmd.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)

    if args.command == "extract":
        return extract_command(args.deck, args.model, args.temperature, args.purpose, args.prompt,
                               args.db, args.raw, args.pages, args.env)

    if not args.folder.is_dir():
        print(f"Folder not found: {args.folder}")
        return 2

    conn = db.connect(args.db)
    try:
        counts = ingest(args.folder, RawStore(args.raw), conn)
    finally:
        conn.close()

    total = sum(counts.values())
    print(f"\n{total} files: " + ", ".join(f"{n} {name}" for name, n in counts.items()))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())