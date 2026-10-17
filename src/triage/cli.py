"""Command line: `triage ingest --folder data/inbox`."""

import argparse
import sqlite3
import sys
from pathlib import Path

from triage import db
from triage.intake.folder import list_files, read_file
from triage.models import PitchState
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage", description="Dealflow triage prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_cmd = commands.add_parser("ingest", help="Ingest every file in a folder")
    ingest_cmd.add_argument("--folder", type=Path, required=True)
    ingest_cmd.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    ingest_cmd.add_argument("--raw", type=Path, default=DEFAULT_RAW_DIR)
    args = parser.parse_args(argv)

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