from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from jev_mail.config import Decision

_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_key  TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    labels       TEXT NOT NULL,
    destination  TEXT
);
CREATE TABLE IF NOT EXISTS uid_keys (
    folder      TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    message_key TEXT NOT NULL,
    PRIMARY KEY (folder, uidvalidity, uid)
);
"""


class ProcessedStore:
    """The only record of what was sorted. Keyed on message identity rather
    than folder and UID, so a message dragged back into a watched folder under
    a new UID is still recognised as handled."""

    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript(_SCHEMA)

    def __enter__(self) -> "ProcessedStore":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def mapped_uids(self, folder: str, uidvalidity: int) -> set[int]:
        rows = self._db.execute("SELECT uid FROM uid_keys WHERE folder = ? AND uidvalidity = ?", (folder, uidvalidity))
        return {uid for (uid,) in rows}

    def map_uids(self, folder: str, uidvalidity: int, keys: dict[int, str]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO uid_keys (folder, uidvalidity, uid, message_key) VALUES (?, ?, ?, ?)",
                [(folder, uidvalidity, uid, key) for uid, key in keys.items()],
            )

    def keys(self, folder: str, uidvalidity: int, uids: Iterable[int]) -> dict[int, str]:
        wanted = list(uids)
        found: dict[int, str] = {}
        for start in range(0, len(wanted), _CHUNK):
            chunk = wanted[start : start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT uid, message_key FROM uid_keys WHERE folder = ? AND uidvalidity = ? AND uid IN ({marks})",
                (folder, uidvalidity, *chunk),
            )
            found.update(rows)
        return found

    def unprocessed_uids(self, folder: str, uidvalidity: int, uids: Iterable[int]) -> list[int]:
        """The given UIDs that are mapped and whose message was never recorded,
        newest first."""
        rows = self._db.execute(
            "SELECT k.uid FROM uid_keys k LEFT JOIN processed p ON p.message_key = k.message_key "
            "WHERE k.folder = ? AND k.uidvalidity = ? AND p.message_key IS NULL",
            (folder, uidvalidity),
        )
        return sorted({uid for (uid,) in rows}.intersection(uids), reverse=True)

    def record(self, message_key: str, decision: Decision) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO processed (message_key, processed_at, labels, destination) VALUES (?, ?, ?, ?)",
                (message_key, datetime.now(timezone.utc).isoformat(), json.dumps(decision.labels), decision.destination),
            )

    def is_processed(self, message_key: str) -> bool:
        return self._db.execute("SELECT 1 FROM processed WHERE message_key = ?", (message_key,)).fetchone() is not None

    def processed_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
