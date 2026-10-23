"""Audit log, idempotent outbox relay and telemetry retention on PostgreSQL.

Relay protocol (at-least-once, idempotent by ``event_id``):

1. *claim* - one transaction marks up to ``batch_size`` unpublished, due and
   unclaimed rows with this relay's owner and a claim deadline
   (``FOR UPDATE SKIP LOCKED``), so concurrent relays never take the same row;
2. *publish* - each claimed event is handed to the sink outside any transaction;
3. *settle* - published rows get ``published_at`` and are never claimed again;
   failed rows keep ``published_at IS NULL`` with ``publish_attempts + 1`` and a
   bounded exponential ``next_attempt_at``.

A crash between 2 and 3 only lets the claim expire, so the event is offered again
with the same ``event_id``. Retention deletes published outbox rows only; the
``audit_events`` trail is append-only at the database level.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import Clock
from src.domain.models.events import AuditEvent
from src.persistence.contracts import EventSink
from src.persistence.events import append_event, event_from_row
from src.persistence.models import AuditEventRecord, OutboxEventRecord
from src.persistence.postgres import PostgresDatabase, Work


class PostgresAuditLog:
    """``AuditLog`` port: audit trail + outbox row in one transaction, idempotent."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def append(self, event: AuditEvent) -> None:
        async def work(session: AsyncSession) -> None:
            exists = await session.scalar(
                select(AuditEventRecord.event_id).where(AuditEventRecord.event_id == event.event_id)
            )
            if exists is None:
                await append_event(session, event)

        await self._db.run_async(work)


@dataclass(frozen=True, slots=True)
class RelayReport:
    claimed: int
    published: int
    failed: int


class PostgresOutboxRelay:
    def __init__(
        self,
        database: PostgresDatabase,
        sink: EventSink,
        clock: Clock,
        *,
        owner: str,
        batch_size: int = 100,
        claim_ttl: timedelta = timedelta(seconds=60),
        base_backoff: timedelta = timedelta(seconds=5),
        max_backoff: timedelta = timedelta(minutes=10),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._db = database
        self._sink = sink
        self._clock = clock
        self._owner = owner
        self._batch_size = batch_size
        self._claim_ttl = claim_ttl
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff

    async def publish_pending(self) -> RelayReport:
        now = self._clock.now()
        claimed = await self._db.run_async(self._claim(now))
        published: list[UUID] = []
        failed: list[tuple[UUID, int, str]] = []
        for attempts, event in claimed:
            try:
                await self._sink.publish(event)
            except Exception as error:
                failed.append((event.event_id, attempts, type(error).__name__))
            else:
                published.append(event.event_id)
        if claimed:
            await self._db.run_async(self._settle(self._clock.now(), published, failed))
        return RelayReport(claimed=len(claimed), published=len(published), failed=len(failed))

    def _claim(self, now: datetime) -> Work[list[tuple[int, AuditEvent]]]:
        async def work(session: AsyncSession) -> list[tuple[int, AuditEvent]]:
            due = (
                select(OutboxEventRecord.event_id)
                .where(
                    OutboxEventRecord.published_at.is_(None),
                    or_(
                        OutboxEventRecord.next_attempt_at.is_(None),
                        OutboxEventRecord.next_attempt_at <= now,
                    ),
                    or_(
                        OutboxEventRecord.claimed_until.is_(None),
                        OutboxEventRecord.claimed_until <= now,
                    ),
                )
                .order_by(OutboxEventRecord.seq)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = await session.execute(
                update(OutboxEventRecord)
                .where(OutboxEventRecord.event_id.in_(due.scalar_subquery()))
                .values(claimed_by=self._owner, claimed_until=now + self._claim_ttl)
                .returning(
                    OutboxEventRecord.seq,
                    OutboxEventRecord.publish_attempts,
                    OutboxEventRecord.event_id,
                    OutboxEventRecord.occurred_at,
                    OutboxEventRecord.aggregate_id,
                    OutboxEventRecord.correlation_id,
                    OutboxEventRecord.causation_id,
                    OutboxEventRecord.schema_version,
                    OutboxEventRecord.event_type,
                    OutboxEventRecord.payload,
                )
            )
            ordered = sorted(rows.all(), key=lambda row: row.seq)
            return [(int(row.publish_attempts), event_from_row(row)) for row in ordered]

        return work

    def _settle(
        self,
        now: datetime,
        published: Sequence[UUID],
        failed: Sequence[tuple[UUID, int, str]],
    ) -> Work[None]:
        async def work(session: AsyncSession) -> None:
            mine = OutboxEventRecord.claimed_by == self._owner
            if published:
                await session.execute(
                    update(OutboxEventRecord)
                    .where(OutboxEventRecord.event_id.in_(published), mine)
                    .values(
                        published_at=now,
                        claimed_by=None,
                        claimed_until=None,
                        last_error=None,
                    )
                )
            for event_id, attempts, error in failed:
                await session.execute(
                    update(OutboxEventRecord)
                    .where(OutboxEventRecord.event_id == event_id, mine)
                    .values(
                        publish_attempts=attempts + 1,
                        next_attempt_at=now + self._backoff(attempts + 1),
                        claimed_by=None,
                        claimed_until=None,
                        last_error=error,
                    )
                )

        return work

    def _backoff(self, attempt: int) -> timedelta:
        delay: timedelta = self._base_backoff * (2 ** min(attempt - 1, 20))
        return min(delay, self._max_backoff)

    async def pending_count(self) -> int:
        async def work(session: AsyncSession) -> int:
            count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(OutboxEventRecord.published_at.is_(None))
            )
            return int(count or 0)

        result: int = await self._db.run_async(work)
        return result


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    published_outbox_age: timedelta = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    outbox_deleted: int


class PostgresRetention:
    """Telemetry clean-up; never touches the audit trail or unpublished events."""

    def __init__(self, database: PostgresDatabase, policy: RetentionPolicy | None = None) -> None:
        self._db = database
        self._policy = policy or RetentionPolicy()

    async def purge(self, now: datetime) -> RetentionReport:
        cutoff = now - self._policy.published_outbox_age

        async def work(session: AsyncSession) -> int:
            deleted = await session.execute(
                delete(OutboxEventRecord)
                .where(
                    and_(
                        OutboxEventRecord.published_at.is_not(None),
                        OutboxEventRecord.published_at < cutoff,
                    )
                )
                .returning(OutboxEventRecord.event_id)
            )
            return len(deleted.all())

        return RetentionReport(outbox_deleted=await self._db.run_async(work))
