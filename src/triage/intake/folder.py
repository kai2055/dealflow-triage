"""Folder adapter: turns each file in a folder into a pitch record."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from triage.intake.email_reader import MAX_ATTACHMENT_BYTES, detect_type, find_links, read_eml
from triage.models import Attachment, PitchState, RawPitch, Source
from triage.storage import RawStore

IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


@dataclass
class FolderItem:
    path: Path
    pitch: RawPitch
    note: str | None = None


def _modified_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _is_ignored(path: Path) -> bool:
    return path.name.startswith(".") or path.name.lower() in IGNORED_NAMES


def read_file(path: Path, store: RawStore) -> FolderItem:
    """Pick the right reader for one file: content first, then the extension."""
    data = path.read_bytes()

    if detect_type(data) == "pdf":
        sha256 = store.save(data)
        deck = Attachment(sha256=sha256, filename=path.name, detected_type="pdf",
                          size_bytes=len(data), supported=len(data) <= MAX_ATTACHMENT_BYTES)
        pitch = RawPitch(source=Source.FOLDER, raw_hash=sha256,
                         received_at=_modified_time(path), attachments=[deck])
        return FolderItem(path, pitch)

    suffix = path.suffix.lower()
    if suffix == ".eml":
        return FolderItem(path, read_eml(data, store, Source.FOLDER))

    if suffix == ".txt":
        text = data.decode("utf-8-sig", errors="replace").strip() or None
        pitch = RawPitch(source=Source.FOLDER, raw_hash=store.save(data),
                         received_at=_modified_time(path), body_text=text,
                         links=find_links(text or ""))
        return FolderItem(path, pitch)

    pitch = RawPitch(source=Source.FOLDER, raw_hash=store.save(data),
                     received_at=_modified_time(path), state=PitchState.FAILED)
    return FolderItem(path, pitch, note=f"unsupported file type: {path.name}")


def read_folder(folder: Path, store: RawStore) -> list[FolderItem]:
    """Read every file at the top level of a folder, in alphabetical order."""
    folder = Path(folder)
    files = [p for p in folder.iterdir() if p.is_file() and not _is_ignored(p)]
    return [read_file(p, store) for p in sorted(files, key=lambda p: p.name.lower())]