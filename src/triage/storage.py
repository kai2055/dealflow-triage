"""Raw storage: keeps every original file exactly as received, named by its fingerprint."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

DEFAULT_RAW_DIR = Path("data/raw")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class CorruptFileError(Exception):
    """A stored file no longer matches its fingerprint."""


def fingerprint(data: bytes) -> str:
    """Return the SHA-256 fingerprint of some bytes, as 64 hex characters."""
    return hashlib.sha256(data).hexdigest()


class RawStore:
    def __init__(self, root: Path = DEFAULT_RAW_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        if not _HASH_RE.match(sha256):
            raise ValueError(f"Not a valid fingerprint: {sha256!r}")
        return self.root / sha256

    def save(self, data: bytes) -> str:
        sha256 = fingerprint(data)
        target = self._path(sha256)
        if target.exists():
            return sha256
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return sha256

    def load(self, sha256: str) -> bytes:
        data = self._path(sha256).read_bytes()
        if fingerprint(data) != sha256:
            raise CorruptFileError(f"File {sha256} does not match its fingerprint")
        return data