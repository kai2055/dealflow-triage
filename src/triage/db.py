"""The pitch register: one row per pitch, its attachments, and every state change."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from triage.models import PitchState, RawPitch

DEFAULT_DB_PATH = Path("data/triage.db")

_STATES = ", ".join(f"'{s.value}'" for s in PitchState)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pitch (
    pitch_id              TEXT PRIMARY KEY,
    source                TEXT NOT NULL CHECK (source IN ('folder', 'gmail')),
    raw_hash              TEXT NOT NULL UNIQUE,
    message_id            TEXT UNIQUE,
    received_at           TEXT NOT NULL,
    sender                TEXT,
    subject               TEXT,
    body_text             TEXT,
    links_json            TEXT NOT NULL DEFAULT '[]',
    state                 TEXT NOT NULL CHECK (state IN ({_STATES})),
    possible_duplicate_of TEXT REFERENCES pitch(pitch_id)
);

CREATE TABLE IF NOT EXISTS attachment (
    pitch_id      TEXT NOT NULL REFERENCES pitch(pitch_id),
    sha256        TEXT NOT NULL,
    filename      TEXT NOT NULL,
    detected_type TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    supported     INTEGER NOT NULL CHECK (supported IN (0, 1)),
    PRIMARY KEY (pitch_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_attachment_sha256 ON attachment(sha256);

CREATE TABLE IF NOT EXISTS state_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pitch_id   TEXT NOT NULL REFERENCES pitch(pitch_id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS extraction (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pitch_id            TEXT NOT NULL REFERENCES pitch(pitch_id),
    purpose             TEXT NOT NULL CHECK (purpose IN ('pipeline', 'bakeoff')),
    started_at          TEXT NOT NULL,
    outcome             TEXT NOT NULL CHECK (outcome IN ('extracted', 'failed')),
    failure             TEXT,
    failure_detail      TEXT,
    prompt_version      TEXT NOT NULL,
    requested_model     TEXT NOT NULL,
    answered_model      TEXT,
    provider            TEXT,
    output_mode         TEXT NOT NULL,
    temperature         REAL,
    data_collection     TEXT NOT NULL,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cost                REAL,
    completeness        REAL,
    quotes_verified     INTEGER,
    quotes_bad          INTEGER,
    quotes_unverifiable INTEGER,
    card_json           TEXT,
    quote_report_json   TEXT,
    raw_text            TEXT
);

CREATE INDEX IF NOT EXISTS idx_extraction_pitch ON extraction(pitch_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def insert_pitch(conn: sqlite3.Connection, pitch: RawPitch, reason: str = "ingested") -> tuple[str, bool]:
    """Store a new pitch. Returns (pitch_id, True), or (existing pitch_id, False) for a duplicate."""
    existing = conn.execute(
        "SELECT pitch_id FROM pitch WHERE raw_hash = ? OR (message_id IS NOT NULL AND message_id = ?)",
        (pitch.raw_hash, pitch.message_id),
    ).fetchone()
    if existing:
        return existing["pitch_id"], False

    pitch_id = str(pitch.pitch_id)
    with conn:
        conn.execute(
            """INSERT INTO pitch (pitch_id, source, raw_hash, message_id, received_at,
                                  sender, subject, body_text, links_json, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pitch_id, pitch.source.value, pitch.raw_hash, pitch.message_id,
             pitch.received_at.isoformat(), pitch.sender, pitch.subject,
             pitch.body_text, json.dumps(pitch.links), pitch.state.value),
        )
        for a in pitch.attachments:
            conn.execute(
                """INSERT OR IGNORE INTO attachment
                   (pitch_id, sha256, filename, detected_type, size_bytes, supported)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pitch_id, a.sha256, a.filename, a.detected_type, a.size_bytes, int(a.supported)),
            )
        conn.execute(
            """INSERT INTO state_history (pitch_id, from_state, to_state, changed_at, reason)
               VALUES (?, NULL, ?, ?, ?)""",
            (pitch_id, pitch.state.value, _now(), reason),
        )
    return pitch_id, True


def set_state(conn: sqlite3.Connection, pitch_id: str, new_state: PitchState,
              reason: str | None = None) -> None:
    row = conn.execute("SELECT state FROM pitch WHERE pitch_id = ?", (pitch_id,)).fetchone()
    if row is None:
        raise KeyError(f"No pitch with id {pitch_id}")
    with conn:
        conn.execute("UPDATE pitch SET state = ? WHERE pitch_id = ?", (new_state.value, pitch_id))
        conn.execute(
            """INSERT INTO state_history (pitch_id, from_state, to_state, changed_at, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (pitch_id, row["state"], new_state.value, _now(), reason),
        )


def mark_possible_duplicate(conn: sqlite3.Connection, pitch_id: str, duplicate_of: str) -> None:
    with conn:
        conn.execute(
            "UPDATE pitch SET possible_duplicate_of = ? WHERE pitch_id = ?",
            (duplicate_of, pitch_id),
        )



def find_pitch_with_deck(conn: sqlite3.Connection, sha256: str, exclude_pitch_id: str) -> str | None:
    """Return the earliest other pitch that has this deck attached, if any."""
    row = conn.execute(
        """SELECT a.pitch_id FROM attachment a JOIN pitch p ON p.pitch_id = a.pitch_id
           WHERE a.sha256 = ? AND a.supported = 1 AND a.pitch_id != ?
           ORDER BY p.rowid LIMIT 1""",
        (sha256, exclude_pitch_id),
    ).fetchone()
    return row["pitch_id"] if row else None


def decks_of_pitch(conn: sqlite3.Connection, pitch_id: str) -> list[str]:
    """Fingerprints of the readable PDF decks attached to a pitch."""
    rows = conn.execute(
        """SELECT sha256 FROM attachment
           WHERE pitch_id = ? AND detected_type = 'pdf' AND supported = 1 ORDER BY filename""",
        (pitch_id,),
    ).fetchall()
    return [r["sha256"] for r in rows]


def insert_extraction(conn: sqlite3.Connection, values: dict) -> int:
    """Save one extraction attempt; attempts are never overwritten (decision 63). Returns its id."""
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with conn:
        cursor = conn.execute(f"INSERT INTO extraction ({columns}) VALUES ({marks})", tuple(values.values()))
    return cursor.lastrowid


def pitches_with_deck_named(conn: sqlite3.Connection, name: str) -> list[tuple[str, str]]:
    """(pitch_id, filename) for every readable deck whose file name, without .pdf, matches `name`."""
    rows = conn.execute(
        "SELECT pitch_id, filename FROM attachment WHERE detected_type = 'pdf' AND supported = 1"
    ).fetchall()
    wanted = name.casefold().removesuffix(".pdf")
    return [(r["pitch_id"], r["filename"]) for r in rows
            if r["filename"].casefold().removesuffix(".pdf") == wanted]