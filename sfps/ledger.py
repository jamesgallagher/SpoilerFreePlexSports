"""SQLite ledger of processed files (design.md §3.1).

A file is fingerprinted by name + size + mtime, so:
- the same file is never processed twice (across restarts too), and
- a *changed* file (re-recorded, different size) is treated as new work.

The fingerprint is taken BEFORE the pipeline runs — the organizer moves the
file, so its identity must be captured while it still exists in /watch.
`unknown` and `error` entries stay queryable for the Phase 7 retry pass.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    fingerprint  TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        INTEGER NOT NULL,
    status       TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class FileIdentity:
    """A file's identity, captured while it still exists in the watch folder."""

    fingerprint: str
    path: str
    size: int
    mtime: int

    @classmethod
    def of(cls, path: Path) -> FileIdentity:
        stat = path.stat()
        raw = f"{path.name}|{stat.st_size}|{int(stat.st_mtime)}"
        return cls(
            fingerprint=hashlib.sha1(raw.encode("utf-8")).hexdigest(),
            path=str(path),
            size=stat.st_size,
            mtime=int(stat.st_mtime),
        )


class Ledger:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def is_processed(self, path: Path) -> bool:
        try:
            identity = FileIdentity.of(path)
        except OSError:
            return False
        return self.has(identity)

    def has(self, identity: FileIdentity) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed WHERE fingerprint = ?", (identity.fingerprint,)
            ).fetchone()
        return row is not None

    def record(
        self, identity: FileIdentity, status: str, target: str = "", detail: str = ""
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed "
                "(fingerprint, path, size, mtime, status, target, detail, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.fingerprint,
                    identity.path,
                    identity.size,
                    identity.mtime,
                    status,
                    target,
                    detail,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
        log.debug("ledger: %s -> %s", Path(identity.path).name, status)

    def entries(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM processed"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query + " ORDER BY processed_at", params)]
