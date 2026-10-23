"""Accounting store contract and its transactional SQLite (file or memory) adapter."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from src.accounting.models import LedgerEntry, ShadowReceipt
from src.domain.models.events import AuditEvent
from src.portfolio.models import PositionOutcome, PositionStatus, ShadowPosition, TimeWindow
from src.portfolio.sqlite import MEMORY, SqliteDatabase, sortable_instant

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_receipts (
    idempotency_key TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_positions (
    position_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_ledger (
    entry_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_outcomes (
    position_id TEXT PRIMARY KEY,
    closed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounting_outbox (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)
"""


class ConcurrentPositionUpdate(RuntimeError):
    """Optimistic concurrency guard rejected a stale lifecycle mutation."""


class AccountingRepository(Protocol):
    def receipt_for(self, idempotency_key: str) -> ShadowReceipt | None: ...

    def position_for(self, position_id: str) -> ShadowPosition | None: ...

    def commit(
        self,
        receipt: ShadowReceipt,
        entries: Sequence[LedgerEntry],
        outcome: PositionOutcome | None,
        events: Sequence[AuditEvent],
        *,
        expected_version: int | None,
    ) -> ShadowReceipt: ...

    def outcomes_in(self, window: TimeWindow) -> tuple[PositionOutcome, ...]: ...


class SqliteAccountingRepository:
    """Restart-safe adapter: receipt, position CAS, ledger, outcome and outbox in one commit."""

    def __init__(self, path: Path | str) -> None:
        self._db = SqliteDatabase(path, _SCHEMA)

    def receipt_for(self, idempotency_key: str) -> ShadowReceipt | None:
        row = self._db.fetch_one(
            "SELECT payload FROM shadow_receipts WHERE idempotency_key = ?", (idempotency_key,)
        )
        return None if row is None else ShadowReceipt.model_validate_json(row[0], strict=False)

    def position_for(self, position_id: str) -> ShadowPosition | None:
        row = self._db.fetch_one(
            "SELECT payload FROM shadow_positions WHERE position_id = ?", (position_id,)
        )
        return None if row is None else ShadowPosition.model_validate_json(row[0], strict=False)

    def commit(
        self,
        receipt: ShadowReceipt,
        entries: Sequence[LedgerEntry],
        outcome: PositionOutcome | None,
        events: Sequence[AuditEvent],
        *,
        expected_version: int | None,
    ) -> ShadowReceipt:
        position = receipt.position
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM shadow_receipts WHERE idempotency_key = ?",
                (receipt.command.idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = ShadowReceipt.model_validate_json(row[0], strict=False)
                if existing.command_digest != receipt.command_digest:
                    raise ValueError("idempotency key is bound to another command")
                return existing
            current = connection.execute(
                "SELECT version, status FROM shadow_positions WHERE position_id = ?",
                (position.position_id,),
            ).fetchone()
            if expected_version is None:
                if current is not None:
                    raise ConcurrentPositionUpdate("position already exists")
            elif (
                current is None
                or int(current[0]) != expected_version
                or current[1] != PositionStatus.OPEN.value
            ):
                raise ConcurrentPositionUpdate("position changed concurrently")
            if position.version != (1 if expected_version is None else expected_version + 1):
                raise ConcurrentPositionUpdate("position version must advance by one")
            try:
                connection.execute(
                    "INSERT INTO shadow_receipts VALUES (?, ?, ?)",
                    (
                        receipt.command.idempotency_key,
                        str(receipt.command.command_id),
                        receipt.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("command id is bound to another idempotency key") from error
            connection.execute(
                "INSERT INTO shadow_positions VALUES (?, ?, ?, ?) "
                "ON CONFLICT(position_id) DO UPDATE SET version = excluded.version, "
                "status = excluded.status, payload = excluded.payload",
                (
                    position.position_id,
                    position.version,
                    position.status.value,
                    position.model_dump_json(),
                ),
            )
            for entry in entries:
                connection.execute(
                    "INSERT INTO shadow_ledger VALUES (?, ?, ?)",
                    (entry.entry_id, entry.position_id, entry.model_dump_json()),
                )
            if outcome is not None:
                connection.execute(
                    "INSERT INTO position_outcomes VALUES (?, ?, ?)",
                    (
                        outcome.position_id,
                        sortable_instant(outcome.closed_at),
                        outcome.model_dump_json(),
                    ),
                )
            for event in events:
                connection.execute(
                    "INSERT INTO accounting_outbox VALUES (?, ?)",
                    (str(event.event_id), event.model_dump_json()),
                )
        return receipt

    def outcomes_in(self, window: TimeWindow) -> tuple[PositionOutcome, ...]:
        rows = self._db.fetch_all(
            "SELECT payload FROM position_outcomes WHERE closed_at >= ? AND closed_at < ? "
            "ORDER BY closed_at, position_id",
            (sortable_instant(window.start), sortable_instant(window.end)),
        )
        return tuple(PositionOutcome.model_validate_json(row[0], strict=False) for row in rows)

    def positions(self) -> tuple[ShadowPosition, ...]:
        rows = self._db.fetch_all("SELECT payload FROM shadow_positions ORDER BY position_id")
        return tuple(ShadowPosition.model_validate_json(row[0], strict=False) for row in rows)

    def ledger_entries(self) -> tuple[LedgerEntry, ...]:
        rows = self._db.fetch_all("SELECT payload FROM shadow_ledger ORDER BY rowid")
        return tuple(LedgerEntry.model_validate_json(row[0], strict=False) for row in rows)

    def outcomes(self) -> tuple[PositionOutcome, ...]:
        rows = self._db.fetch_all("SELECT payload FROM position_outcomes ORDER BY rowid")
        return tuple(PositionOutcome.model_validate_json(row[0], strict=False) for row in rows)

    def outbox_events(self) -> tuple[AuditEvent, ...]:
        rows = self._db.fetch_all("SELECT payload FROM accounting_outbox ORDER BY rowid")
        return tuple(AuditEvent.model_validate_json(row[0]) for row in rows)

    def close(self) -> None:
        self._db.close()


class InMemoryAccountingRepository(SqliteAccountingRepository):
    """Same transactional adapter on a private in-memory database (tests, shadow runs)."""

    def __init__(self) -> None:
        super().__init__(MEMORY)
