"""PostgreSQL universe history, decision records and position context.

* ``PostgresUniverseStore`` implements the T02 ``UniverseSnapshotStore``: a sync is
  staged in memory and published in one transaction (a partial sync never replaces
  the last complete universe), snapshots are immutable and idempotent per
  ``(pool, observed_at)``.
* ``PostgresDecisionStore`` writes immutable decision and plan records with
  deterministic identifiers, so a retried cycle never duplicates them.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import ScalarResult, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market.models import PoolRef, PoolSnapshot
from src.persistence.contracts import DecisionRecord, PlanRecord, PositionContext
from src.persistence.models import (
    CandidateDecisionRecord,
    FeatureVectorRecord,
    PoolRecord,
    PoolSnapshotRecord,
    PositionContextRecord,
    StrategyPlanRecord,
    UniverseMemberRecord,
    UniverseSyncRecord,
)
from src.persistence.postgres import PostgresDatabase
from src.persistence.serialization import from_payload, to_payload


def as_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"correlation:{value}")


def pool_id_for(address: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"meteora-volume-bot:pool:{address}")


def snapshot_id_for(address: str, observed_at: datetime) -> UUID:
    return uuid5(
        NAMESPACE_URL, f"meteora-volume-bot:pool-snapshot:{address}:{observed_at.isoformat()}"
    )


async def _ensure_pool(session: AsyncSession, pool: PoolRef) -> UUID:
    await session.execute(
        pg_insert(PoolRecord)
        .values(
            id=pool_id_for(pool.address.root),
            address=pool.address.root,
            base_mint=pool.base_mint.root,
            quote_mint=pool.quote_mint.root,
        )
        .on_conflict_do_nothing(index_elements=[PoolRecord.address])
    )
    pool_id = await session.scalar(
        select(PoolRecord.id).where(PoolRecord.address == pool.address.root)
    )
    if pool_id is None:  # pragma: no cover - guarded by the upsert above
        raise RuntimeError("pool row disappeared")
    return pool_id


def _window_total(snapshot: PoolSnapshot, field: str) -> Decimal:
    window = snapshot.windows.get("24h")
    if window is None:
        return Decimal(0)
    value = getattr(window, field)
    return value if value is not None else Decimal(0)


async def _ensure_snapshot(session: AsyncSession, snapshot: PoolSnapshot) -> tuple[UUID, bool]:
    """Insert an immutable snapshot; returns ``(id, inserted)``."""

    pool_id = await _ensure_pool(session, snapshot.pool)
    inserted = await session.execute(
        pg_insert(PoolSnapshotRecord)
        .values(
            id=snapshot_id_for(snapshot.pool.address.root, snapshot.observed_at),
            pool_id=pool_id,
            observed_at=snapshot.observed_at,
            tvl=snapshot.tvl,
            volume=_window_total(snapshot, "volume"),
            fees=_window_total(snapshot, "fees"),
            price=snapshot.price,
            data_quality=snapshot.data_quality.value,
            raw_snapshot=to_payload(snapshot),
        )
        .on_conflict_do_nothing(
            index_elements=[PoolSnapshotRecord.pool_id, PoolSnapshotRecord.observed_at]
        )
        .returning(PoolSnapshotRecord.id)
    )
    new_id = inserted.scalar_one_or_none()
    if new_id is not None:
        return new_id, True
    existing = await session.scalar(
        select(PoolSnapshotRecord.id).where(
            PoolSnapshotRecord.pool_id == pool_id,
            PoolSnapshotRecord.observed_at == snapshot.observed_at,
        )
    )
    if existing is None:  # pragma: no cover - guarded by the unique constraint
        raise RuntimeError("pool snapshot row disappeared")
    return existing, False


class _PostgresUniverseStage:
    def __init__(self, database: PostgresDatabase, sync_id: UUID, observed_at: datetime) -> None:
        self._db = database
        self._sync_id = sync_id
        self._observed_at = observed_at
        self._snapshots: list[PoolSnapshot] = []
        self._addresses: set[str] = set()
        self._closed = False

    async def stage(self, snapshots: tuple[PoolSnapshot, ...]) -> None:
        self._ensure_open()
        for snapshot in snapshots:
            address = snapshot.pool.address.root
            if address in self._addresses:
                raise ValueError("universe version contains duplicate pools")
            if snapshot.observed_at != self._observed_at:
                raise ValueError("universe version contains mixed observation times")
            self._addresses.add(address)
            self._snapshots.append(snapshot)

    async def commit(self) -> int:
        self._ensure_open()
        snapshots = tuple(self._snapshots)

        async def work(session: AsyncSession) -> int:
            duplicates = 0
            members: list[UUID] = []
            for snapshot in snapshots:
                snapshot_id, inserted = await _ensure_snapshot(session, snapshot)
                duplicates += 0 if inserted else 1
                members.append(snapshot_id)
            await session.execute(
                insert(UniverseSyncRecord).values(
                    sync_id=self._sync_id,
                    observed_at=self._observed_at,
                    pool_count=len(snapshots),
                    duplicate_count=duplicates,
                )
            )
            for snapshot_id in members:
                await session.execute(
                    insert(UniverseMemberRecord).values(
                        sync_id=self._sync_id, pool_snapshot_id=snapshot_id
                    )
                )
            return duplicates

        duplicates: int = await self._db.run_async(work)
        self._closed = True
        self._snapshots.clear()
        self._addresses.clear()
        return duplicates

    async def abort(self) -> None:
        self._closed = True
        self._snapshots.clear()
        self._addresses.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("universe snapshot stage is already closed")


class PostgresUniverseStore:
    """``UniverseSnapshotStore`` and ``SnapshotHistory`` over the T01 market tables."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def begin(self, sync_id: UUID, observed_at: datetime) -> _PostgresUniverseStage:
        return _PostgresUniverseStage(self._db, sync_id, observed_at)

    async def history(
        self, pool_address: str, *, until: datetime, limit: int
    ) -> tuple[PoolSnapshot, ...]:
        async def work(session: AsyncSession) -> tuple[PoolSnapshot, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PoolSnapshotRecord.raw_snapshot)
                .join(PoolRecord, PoolRecord.id == PoolSnapshotRecord.pool_id)
                .where(PoolRecord.address == pool_address, PoolSnapshotRecord.observed_at <= until)
                .order_by(PoolSnapshotRecord.observed_at.desc())
                .limit(limit)
            )
            return tuple(reversed([from_payload(PoolSnapshot, payload) for payload in payloads]))

        result: tuple[PoolSnapshot, ...] = await self._db.run_async(work)
        return result

    async def latest_universe(self) -> tuple[PoolSnapshot, ...]:
        async def work(session: AsyncSession) -> tuple[PoolSnapshot, ...]:
            sync_id = await session.scalar(
                select(UniverseSyncRecord.sync_id)
                .order_by(
                    UniverseSyncRecord.observed_at.desc(), UniverseSyncRecord.committed_at.desc()
                )
                .limit(1)
            )
            if sync_id is None:
                return ()
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PoolSnapshotRecord.raw_snapshot)
                .join(
                    UniverseMemberRecord,
                    UniverseMemberRecord.pool_snapshot_id == PoolSnapshotRecord.id,
                )
                .join(PoolRecord, PoolRecord.id == PoolSnapshotRecord.pool_id)
                .where(UniverseMemberRecord.sync_id == sync_id)
                .order_by(PoolRecord.address)
            )
            return tuple(from_payload(PoolSnapshot, payload) for payload in payloads)

        result: tuple[PoolSnapshot, ...] = await self._db.run_async(work)
        return result

    async def latest_sync_at(self) -> datetime | None:
        async def work(session: AsyncSession) -> datetime | None:
            return await session.scalar(
                select(UniverseSyncRecord.observed_at)
                .order_by(UniverseSyncRecord.observed_at.desc())
                .limit(1)
            )

        result: datetime | None = await self._db.run_async(work)
        return result


class PostgresDecisionStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def record_decision(self, record: DecisionRecord) -> UUID:
        decision = record.decision

        async def work(session: AsyncSession) -> UUID:
            snapshot_id, _ = await _ensure_snapshot(session, record.market)
            await session.execute(
                pg_insert(FeatureVectorRecord)
                .values(
                    id=uuid5(
                        NAMESPACE_URL, f"feature-vector:{snapshot_id}:{decision.ruleset_version}"
                    ),
                    pool_snapshot_id=snapshot_id,
                    ruleset_version=decision.ruleset_version,
                    values=to_payload(decision.features),
                )
                .on_conflict_do_nothing()
            )
            feature_id = await session.scalar(
                select(FeatureVectorRecord.id).where(
                    FeatureVectorRecord.pool_snapshot_id == snapshot_id,
                    FeatureVectorRecord.ruleset_version == decision.ruleset_version,
                )
            )
            await session.execute(
                pg_insert(CandidateDecisionRecord)
                .values(
                    id=record.decision_id,
                    pool_id=pool_id_for(decision.pool_address.root),
                    feature_vector_id=feature_id,
                    status=decision.status.value,
                    score=decision.score,
                    reasons=[reason.value for reason in decision.reasons],
                    missing_signals=list(decision.missing_signals),
                    evidence=to_payload(record),
                    ruleset_version=decision.ruleset_version,
                    correlation_id=as_uuid(decision.correlation_id),
                    decided_at=decision.decision_at,
                    mode=decision.mode.value,
                    config_version=decision.config_version,
                )
                .on_conflict_do_nothing(index_elements=[CandidateDecisionRecord.id])
            )
            return record.decision_id

        result: UUID = await self._db.run_async(work)
        return result

    async def record_plan(self, record: PlanRecord) -> UUID:
        plan = record.sized_plan.plan

        async def work(session: AsyncSession) -> UUID:
            await session.execute(
                pg_insert(StrategyPlanRecord)
                .values(
                    id=record.plan_id,
                    decision_id=record.decision_id,
                    strategy=plan.strategy.value,
                    parameters=to_payload(record),
                    ruleset_version=plan.ruleset_version,
                    correlation_id=as_uuid(plan.correlation_id),
                    created_at=record.recorded_at,
                )
                .on_conflict_do_nothing(index_elements=[StrategyPlanRecord.id])
            )
            return record.plan_id

        result: UUID = await self._db.run_async(work)
        return result

    async def decision(self, decision_id: UUID) -> DecisionRecord | None:
        async def work(session: AsyncSession) -> DecisionRecord | None:
            payload = await session.scalar(
                select(CandidateDecisionRecord.evidence).where(
                    CandidateDecisionRecord.id == decision_id
                )
            )
            return None if payload is None else from_payload(DecisionRecord, payload)

        result: DecisionRecord | None = await self._db.run_async(work)
        return result

    async def plan(self, plan_id: UUID) -> PlanRecord | None:
        async def work(session: AsyncSession) -> PlanRecord | None:
            payload = await session.scalar(
                select(StrategyPlanRecord.parameters).where(StrategyPlanRecord.id == plan_id)
            )
            return None if payload is None else from_payload(PlanRecord, payload)

        result: PlanRecord | None = await self._db.run_async(work)
        return result


class PostgresPositionContexts:
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def put(self, context: PositionContext) -> None:
        async def work(session: AsyncSession) -> None:
            existing = await session.scalar(
                select(PositionContextRecord.payload).where(
                    PositionContextRecord.position_id == context.position_id
                )
            )
            if existing is not None:
                if from_payload(PositionContext, existing) != context:
                    raise ValueError("position context is immutable")
                return
            await session.execute(
                insert(PositionContextRecord).values(
                    position_id=context.position_id,
                    plan_id=context.plan_id,
                    pool_address=context.pool_address,
                    payload=to_payload(context),
                )
            )

        await self._db.run_async(work)

    async def get(self, position_id: str) -> PositionContext | None:
        async def work(session: AsyncSession) -> PositionContext | None:
            payload = await session.scalar(
                select(PositionContextRecord.payload).where(
                    PositionContextRecord.position_id == position_id
                )
            )
            return None if payload is None else from_payload(PositionContext, payload)

        result: PositionContext | None = await self._db.run_async(work)
        return result

    async def all(self) -> tuple[PositionContext, ...]:
        async def work(session: AsyncSession) -> tuple[PositionContext, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PositionContextRecord.payload).order_by(PositionContextRecord.position_id)
            )
            return tuple(from_payload(PositionContext, payload) for payload in payloads)

        result: tuple[PositionContext, ...] = await self._db.run_async(work)
        return result
