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
with the same ``event_id``. Retention deletes published outbox rows and telemetry history
older than its window (``PostgresRetention``); the ``audit_events`` trail is append-only
at the database level.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.core.clock import Clock
from src.domain.models.events import AuditEvent
from src.persistence.contracts import EventSink
from src.persistence.events import append_event, event_from_row
from src.persistence.models import (
    AuditEventRecord,
    CandidateDecisionHeadRecord,
    CandidateDecisionRecord,
    FeatureVectorRecord,
    OutboxEventRecord,
    PoolSnapshotRecord,
    StrategyPlanRecord,
    UniverseMemberRecord,
    UniverseSyncRecord,
)
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


HISTORY_RETENTION_ENV = "HISTORY_RETENTION_HOURS"
UNIVERSE_INTERVAL_ENV = "WORKER_UNIVERSE_INTERVAL_SECONDS"
DEFAULT_HISTORY_WINDOW = timedelta(hours=36)
# The features read up to ~300 points of a pool's history (``maximum_history_points``):
# the window never drops below that many universe intervals, whatever is configured.
HISTORY_POINTS_FLOOR = 300
_DEFAULT_UNIVERSE_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long telemetry lives (R29i.2). The audit trail is never purged."""

    published_outbox_age: timedelta = timedelta(days=7)
    # Snapshots, universe versions, feature vectors and decisions without a plan.
    history_window: timedelta = DEFAULT_HISTORY_WINDOW
    batch_size: int = 5_000

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "RetentionPolicy":
        """``HISTORY_RETENTION_HOURS`` (36 by default), never shorter than 300 intervals of
        ``WORKER_UNIVERSE_INTERVAL_SECONDS``."""

        raw_hours = (environ.get(HISTORY_RETENTION_ENV) or "").strip()
        window = timedelta(hours=float(raw_hours)) if raw_hours else DEFAULT_HISTORY_WINDOW
        if window <= timedelta(0):
            raise ValueError(f"{HISTORY_RETENTION_ENV} must be a positive number of hours")
        raw_interval = (environ.get(UNIVERSE_INTERVAL_ENV) or "").strip()
        interval = (
            timedelta(seconds=int(raw_interval)) if raw_interval else _DEFAULT_UNIVERSE_INTERVAL
        )
        return cls(history_window=max(window, interval * HISTORY_POINTS_FLOOR))


@dataclass(frozen=True, slots=True)
class RetentionReport:
    outbox_deleted: int
    snapshots_deleted: int = 0
    universe_syncs_deleted: int = 0
    feature_vectors_deleted: int = 0
    decisions_deleted: int = 0
    heads_deleted: int = 0


class PostgresRetention:
    """Telemetry clean-up; never touches the audit trail, unpublished events or a record a
    money step depends on.

    Older than ``history_window`` and deleted in bounded batches (one transaction each):

    * decision heads of pools no cycle evaluated within the window;
    * decisions with no plan that no head stands on (a plan's decision is kept for good);
    * feature vectors no kept decision references;
    * universe versions except the newest one (``latest_universe`` reads it);
    * pool snapshots no kept universe version or feature vector references, except each
      pool's newest one (an open position whose pool left the universe is valued by it);
    * published outbox rows older than ``published_outbox_age``.
    """

    def __init__(self, database: PostgresDatabase, policy: RetentionPolicy | None = None) -> None:
        self._db = database
        self._policy = policy or RetentionPolicy()

    async def purge(self, now: datetime) -> RetentionReport:
        cutoff = now - self._policy.history_window
        heads = await self._db.run_async(_delete_stale_heads(cutoff))
        decisions = await self._batches(_delete_unplanned_decisions(cutoff, self._batch))
        features = await self._batches(_delete_orphan_features(cutoff, self._batch))
        syncs = await self._batches(_delete_old_universes(cutoff, _UNIVERSES_PER_BATCH))
        snapshots = await self._batches(_delete_old_snapshots(cutoff, self._batch))
        outbox = await self._db.run_async(
            _delete_published_outbox(now - self._policy.published_outbox_age)
        )
        return RetentionReport(
            outbox_deleted=outbox,
            snapshots_deleted=snapshots,
            universe_syncs_deleted=syncs,
            feature_vectors_deleted=features,
            decisions_deleted=decisions,
            heads_deleted=heads,
        )

    @property
    def _batch(self) -> int:
        return self._policy.batch_size

    async def _batches(self, work: tuple[Work[int], int]) -> int:
        statement, size = work
        total = 0
        while True:
            deleted = await self._db.run_async(statement)
            total += deleted
            if deleted < size:
                return total


# Each universe version holds one member row per pool (~2000): a few versions per batch.
_UNIVERSES_PER_BATCH = 10


def _delete_stale_heads(cutoff: datetime) -> Work[int]:
    async def work(session: AsyncSession) -> int:
        deleted = await session.execute(
            delete(CandidateDecisionHeadRecord)
            .where(CandidateDecisionHeadRecord.evaluated_at < cutoff)
            .returning(CandidateDecisionHeadRecord.pool_id)
        )
        return len(deleted.all())

    return work


def _delete_unplanned_decisions(cutoff: datetime, size: int) -> tuple[Work[int], int]:
    decision = CandidateDecisionRecord

    async def work(session: AsyncSession) -> int:
        doomed = (
            select(decision.id)
            .where(
                decision.decided_at < cutoff,
                ~exists().where(StrategyPlanRecord.decision_id == decision.id),
                ~exists().where(CandidateDecisionHeadRecord.decision_id == decision.id),
            )
            .limit(size)
            .scalar_subquery()
        )
        deleted = await session.execute(
            delete(decision).where(decision.id.in_(doomed)).returning(decision.id)
        )
        return len(deleted.all())

    return work, size


def _delete_orphan_features(cutoff: datetime, size: int) -> tuple[Work[int], int]:
    features = FeatureVectorRecord

    async def work(session: AsyncSession) -> int:
        doomed = (
            select(features.id)
            .join(PoolSnapshotRecord, PoolSnapshotRecord.id == features.pool_snapshot_id)
            .where(
                PoolSnapshotRecord.observed_at < cutoff,
                ~exists().where(CandidateDecisionRecord.feature_vector_id == features.id),
            )
            .limit(size)
            .scalar_subquery()
        )
        deleted = await session.execute(
            delete(features).where(features.id.in_(doomed)).returning(features.id)
        )
        return len(deleted.all())

    return work, size


def _delete_old_universes(cutoff: datetime, size: int) -> tuple[Work[int], int]:
    syncs = UniverseSyncRecord

    async def work(session: AsyncSession) -> int:
        newest = await session.scalar(
            select(syncs.sync_id)
            .order_by(syncs.observed_at.desc(), syncs.committed_at.desc())
            .limit(1)
        )
        if newest is None:
            return 0
        doomed = list(
            await session.scalars(
                select(syncs.sync_id)
                .where(syncs.observed_at < cutoff, syncs.sync_id != newest)
                .order_by(syncs.observed_at)
                .limit(size)
            )
        )
        if not doomed:
            return 0
        await session.execute(
            delete(UniverseMemberRecord).where(UniverseMemberRecord.sync_id.in_(doomed))
        )
        await session.execute(delete(syncs).where(syncs.sync_id.in_(doomed)))
        return len(doomed)

    return work, size


def _delete_old_snapshots(cutoff: datetime, size: int) -> tuple[Work[int], int]:
    snapshot = PoolSnapshotRecord
    newer = aliased(PoolSnapshotRecord)

    async def work(session: AsyncSession) -> int:
        doomed = (
            select(snapshot.id)
            .where(
                snapshot.observed_at < cutoff,
                exists().where(
                    newer.pool_id == snapshot.pool_id, newer.observed_at > snapshot.observed_at
                ),
                ~exists().where(UniverseMemberRecord.pool_snapshot_id == snapshot.id),
                ~exists().where(FeatureVectorRecord.pool_snapshot_id == snapshot.id),
            )
            .limit(size)
            .scalar_subquery()
        )
        deleted = await session.execute(
            delete(snapshot).where(snapshot.id.in_(doomed)).returning(snapshot.id)
        )
        return len(deleted.all())

    return work, size


def _delete_published_outbox(cutoff: datetime) -> Work[int]:
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

    return work
