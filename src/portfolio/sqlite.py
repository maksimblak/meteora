"""Single-connection SQLite store shared by the accounting and mode adapters.

Every mutation runs inside ``BEGIN IMMEDIATE``: the write lock is taken before
any guard is read, so check-then-write sequences stay atomic even when several
processes or connections share one database file.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

MEMORY = ":memory:"


class SqliteDatabase:
    def __init__(self, path: Path | str, schema: str) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        if str(path) != MEMORY:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        with self.transaction() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchone()

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def sortable_instant(value: datetime) -> str:
    """UTC timestamp text whose lexical order equals chronological order."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stored timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
