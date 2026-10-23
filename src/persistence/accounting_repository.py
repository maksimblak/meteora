"""PostgreSQL adapter of the T04 ``AccountingRepository`` contract (D01).

Receipt, position compare-and-set, ledger, outcome and events commit in one
transaction serialized by the accounting advisory lock, mirroring the SQLite
adapter's ``BEGIN IMMEDIATE`` boundary.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ScalarResult, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.accounting.models import LedgerEntry, ShadowReceipt
from src.accounting.repository import ConcurrentPositionUpdate
from src.domain.models.events import AuditEvent, EventType
from src.persistence.events import append_event, events_of_types
from src.persistence.models import (
    PositionOutcomeRecord,
    ShadowLedgerRecord,
    ShadowPositionRecord,
    ShadowReceiptRecord,
)
from src.persistence.postgres import PostgresDatabase, advisory_key
from src.persistence.serialization import from_payload, to_payload
from src.portfolio.models import PositionOutcome, PositionStatus, ShadowPosition, TimeWindow

ACCOUNTING_LOCK = advisory_key("meteora-volume-bot:accounting-repository")
ACCOUNTING_EVENT_TYPES = frozenset(
    {
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_OPENED,
        EventType.POSITION_CLOSED,
    }
)


class PostgresAccountingRepository:
    def __init__(self, database: PostgresDatabase, *, owns_database: bool = False) -> None:
        self._db = database
        self._owns_database = owns_database

    def receipt_for(self, idempotency_key: str) -> ShadowReceipt | None:
        async def work(session: AsyncSession) -> ShadowReceipt | None:
            return await _receipt(session, idempotency_key)

        result: ShadowReceipt | None = self._db.run(work)
        return result

    def position_for(self, position_id: str) -> ShadowPosition | None:
        async def work(session: AsyncSession) -> ShadowPosition | None:
            payload = await session.scalar(
                select(ShadowPositionRecord.payload).where(
                    ShadowPositionRecord.position_id == position_id
                )
            )
            return None if payload is None else from_payload(ShadowPosition, payload)

        result: ShadowPosition | None = self._db.run(work)
        return result

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

        async def work(session: AsyncSession) -> ShadowReceipt:
            existing = await _receipt(session, receipt.command.idempotency_key)
            if existing is not None:
                if existing.command_digest != receipt.command_digest:
                    raise ValueError("idempotency key is bound to another command")
                return existing
            current = (
                await session.execute(
                    select(ShadowPositionRecord.version, ShadowPositionRecord.status).where(
                        ShadowPositionRecord.position_id == position.position_id
                    )
                )
            ).one_or_none()
            if expected_version is None:
                if current is not None:
                    raise ConcurrentPositionUpdate("position already exists")
            elif (
                current is None
                or int(current.version) != expected_version
                or current.status != PositionStatus.OPEN.value
            ):
                raise ConcurrentPositionUpdate("position changed concurrently")
            if position.version != (1 if expected_version is None else expected_version + 1):
                raise ConcurrentPositionUpdate("position version must advance by one")
            bound = await session.scalar(
                select(ShadowReceiptRecord.idempotency_key).where(
                    ShadowReceiptRecord.command_id == receipt.command.command_id
                )
            )
            if bound is not None:
                raise ValueError("command id is bound to another idempotency key")
            await session.execute(
                insert(ShadowReceiptRecord).values(
                    idempotency_key=receipt.command.idempotency_key,
                    command_id=receipt.command.command_id,
                    command_digest=receipt.command_digest,
                    payload=to_payload(receipt),
                )
            )
            values = {
                "version": position.version,
                "status": position.status.value,
                "payload": to_payload(position),
            }
            await session.execute(
                pg_insert(ShadowPositionRecord)
                .values(position_id=position.position_id, **values)
                .on_conflict_do_update(
                    index_elements=[ShadowPositionRecord.position_id], set_=values
                )
            )
            for entry in entries:
                await session.execute(
                    insert(ShadowLedgerRecord).values(
                        entry_id=entry.entry_id,
                        position_id=entry.position_id,
                        payload=to_payload(entry),
                    )
                )
            if outcome is not None:
                await session.execute(
                    insert(PositionOutcomeRecord).values(
                        position_id=outcome.position_id,
                        closed_at=outcome.closed_at,
                        payload=to_payload(outcome),
                    )
                )
            for event in events:
                await append_event(session, event)
            return receipt

        result: ShadowReceipt = self._db.run(work, lock=ACCOUNTING_LOCK)
        return result

    def outcomes_in(self, window: TimeWindow) -> tuple[PositionOutcome, ...]:
        async def work(session: AsyncSession) -> tuple[PositionOutcome, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PositionOutcomeRecord.payload)
                .where(
                    PositionOutcomeRecord.closed_at >= window.start,
                    PositionOutcomeRecord.closed_at < window.end,
                )
                .order_by(PositionOutcomeRecord.closed_at, PositionOutcomeRecord.position_id)
            )
            return tuple(from_payload(PositionOutcome, payload) for payload in payloads)

        result: tuple[PositionOutcome, ...] = self._db.run(work)
        return result

    # --- views ------------------------------------------------------------------

    def positions(self) -> tuple[ShadowPosition, ...]:
        async def work(session: AsyncSession) -> tuple[ShadowPosition, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(ShadowPositionRecord.payload).order_by(ShadowPositionRecord.position_id)
            )
            return tuple(from_payload(ShadowPosition, payload) for payload in payloads)

        result: tuple[ShadowPosition, ...] = self._db.run(work)
        return result

    def open_positions(self) -> tuple[ShadowPosition, ...]:
        async def work(session: AsyncSession) -> tuple[ShadowPosition, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(ShadowPositionRecord.payload)
                .where(ShadowPositionRecord.status == PositionStatus.OPEN.value)
                .order_by(ShadowPositionRecord.position_id)
            )
            return tuple(from_payload(ShadowPosition, payload) for payload in payloads)

        result: tuple[ShadowPosition, ...] = self._db.run(work)
        return result

    def ledger_entries(self) -> tuple[LedgerEntry, ...]:
        async def work(session: AsyncSession) -> tuple[LedgerEntry, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(ShadowLedgerRecord.payload).order_by(ShadowLedgerRecord.seq)
            )
            return tuple(from_payload(LedgerEntry, payload) for payload in payloads)

        result: tuple[LedgerEntry, ...] = self._db.run(work)
        return result

    def outcomes(self) -> tuple[PositionOutcome, ...]:
        async def work(session: AsyncSession) -> tuple[PositionOutcome, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PositionOutcomeRecord.payload).order_by(PositionOutcomeRecord.seq)
            )
            return tuple(from_payload(PositionOutcome, payload) for payload in payloads)

        result: tuple[PositionOutcome, ...] = self._db.run(work)
        return result

    def outbox_events(self) -> tuple[AuditEvent, ...]:
        async def work(session: AsyncSession) -> tuple[AuditEvent, ...]:
            return await events_of_types(session, ACCOUNTING_EVENT_TYPES)

        result: tuple[AuditEvent, ...] = self._db.run(work)
        return result

    def close(self) -> None:
        if self._owns_database:
            self._db.close()


async def _receipt(session: AsyncSession, idempotency_key: str) -> ShadowReceipt | None:
    payload = await session.scalar(
        select(ShadowReceiptRecord.payload).where(
            ShadowReceiptRecord.idempotency_key == idempotency_key
        )
    )
    return None if payload is None else from_payload(ShadowReceipt, payload)
