"""Read-side queries over decision records that ``DecisionStore`` does not offer.

``DecisionStore`` (T07) only fetches one record by id; the operator panel needs
newest-first pagination, the plan of a decision and the latest decision per pool.
These are read-only projections over the same records: nothing here writes, and
only the immutable domain records (``DecisionRecord``/``PlanRecord``) leave it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.screening import CandidateStatus
from src.persistence.contracts import DecisionRecord, PlanRecord
from src.persistence.factory import Repositories
from src.persistence.market_store import pool_id_for
from src.persistence.memory import InMemoryDecisionStore
from src.persistence.models import CandidateDecisionRecord, PoolRecord, StrategyPlanRecord
from src.persistence.postgres import PostgresDatabase
from src.persistence.serialization import from_payload


@dataclass(frozen=True, slots=True)
class DecisionQuery:
    limit: int
    cursor: int | None = None
    status: CandidateStatus | None = None
    pool_address: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionPage:
    items: tuple[DecisionRecord, ...]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    pool_address: str
    status: CandidateStatus
    reasons: tuple[str, ...]
    decided_at: datetime


class DecisionCatalog(Protocol):
    async def page(self, query: DecisionQuery) -> DecisionPage: ...

    async def plan_for(self, decision_id: UUID) -> PlanRecord | None: ...

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]: ...

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]: ...


class UnsupportedCatalog(RuntimeError):
    pass


def decision_catalog(repositories: Repositories) -> DecisionCatalog:
    if repositories.database is not None:
        return PostgresDecisionCatalog(repositories.database)
    if isinstance(repositories.decisions, InMemoryDecisionStore):
        return MemoryDecisionCatalog(repositories.decisions)
    raise UnsupportedCatalog("no decision read model for this persistence backend")


class MemoryDecisionCatalog:
    """Local-debugging backend: records in insertion order, cursor is an offset."""

    def __init__(self, store: InMemoryDecisionStore) -> None:
        self._store = store

    async def page(self, query: DecisionQuery) -> DecisionPage:
        records = [
            record
            for record in reversed(tuple(self._store.decisions.values()))
            if (query.status is None or record.decision.status is query.status)
            and (
                query.pool_address is None
                or record.decision.pool_address.root == query.pool_address
            )
        ]
        start = query.cursor or 0
        items = tuple(records[start : start + query.limit])
        following = start + query.limit
        return DecisionPage(items, following if following < len(records) else None)

    async def plan_for(self, decision_id: UUID) -> PlanRecord | None:
        plans = [plan for plan in self._store.plans.values() if plan.decision_id == decision_id]
        return plans[-1] if plans else None

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]:
        wanted = set(addresses)
        latest: dict[str, DecisionRecord] = {}
        for record in self._store.decisions.values():
            address = record.decision.pool_address.root
            if address in wanted:
                latest[address] = record
        return latest

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]:
        latest: dict[str, DecisionRecord] = {}
        for record in self._store.decisions.values():
            latest[record.decision.pool_address.root] = record
        return tuple(
            DecisionOutcome(
                pool_address=address,
                status=record.decision.status,
                reasons=tuple(reason.value for reason in record.decision.reasons),
                decided_at=record.decision.decision_at,
            )
            for address, record in latest.items()
        )


class PostgresDecisionCatalog:
    """Keyset pagination on the decision sequence; the cursor is the last ``seq`` seen."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def page(self, query: DecisionQuery) -> DecisionPage:
        async def work(session: AsyncSession) -> DecisionPage:
            statement = (
                select(CandidateDecisionRecord.seq, CandidateDecisionRecord.evidence)
                .order_by(CandidateDecisionRecord.seq.desc())
                .limit(query.limit + 1)
            )
            if query.cursor is not None:
                statement = statement.where(CandidateDecisionRecord.seq < query.cursor)
            if query.status is not None:
                statement = statement.where(CandidateDecisionRecord.status == query.status.value)
            if query.pool_address is not None:
                statement = statement.where(
                    CandidateDecisionRecord.pool_id == pool_id_for(query.pool_address)
                )
            rows = (await session.execute(statement)).all()
            visible = rows[: query.limit]
            items = tuple(from_payload(DecisionRecord, row.evidence) for row in visible)
            more = len(rows) > query.limit and bool(visible)
            return DecisionPage(items, int(visible[-1].seq) if more else None)

        result: DecisionPage = await self._db.run_async(work)
        return result

    async def plan_for(self, decision_id: UUID) -> PlanRecord | None:
        async def work(session: AsyncSession) -> PlanRecord | None:
            payload = await session.scalar(
                select(StrategyPlanRecord.parameters)
                .where(StrategyPlanRecord.decision_id == decision_id)
                .order_by(StrategyPlanRecord.created_at.desc())
                .limit(1)
            )
            return None if payload is None else from_payload(PlanRecord, payload)

        result: PlanRecord | None = await self._db.run_async(work)
        return result

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]:
        if not addresses:
            return {}

        async def work(session: AsyncSession) -> dict[str, DecisionRecord]:
            rows = await session.execute(
                select(PoolRecord.address, CandidateDecisionRecord.evidence)
                .join(PoolRecord, PoolRecord.id == CandidateDecisionRecord.pool_id)
                .where(PoolRecord.address.in_(list(addresses)))
                .order_by(CandidateDecisionRecord.pool_id, CandidateDecisionRecord.seq.desc())
                .ext(distinct_on(CandidateDecisionRecord.pool_id))
            )
            return {row.address: from_payload(DecisionRecord, row.evidence) for row in rows}

        result: dict[str, DecisionRecord] = await self._db.run_async(work)
        return result

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]:
        async def work(session: AsyncSession) -> tuple[DecisionOutcome, ...]:
            rows = await session.execute(
                select(
                    PoolRecord.address,
                    CandidateDecisionRecord.status,
                    CandidateDecisionRecord.reasons,
                    CandidateDecisionRecord.decided_at,
                )
                .join(PoolRecord, PoolRecord.id == CandidateDecisionRecord.pool_id)
                .order_by(CandidateDecisionRecord.pool_id, CandidateDecisionRecord.seq.desc())
                .ext(distinct_on(CandidateDecisionRecord.pool_id))
            )
            return tuple(_outcome(row) for row in rows)

        result: tuple[DecisionOutcome, ...] = await self._db.run_async(work)
        return result


def _outcome(row: Any) -> DecisionOutcome:
    return DecisionOutcome(
        pool_address=row.address,
        status=CandidateStatus(row.status),
        reasons=tuple(str(reason) for reason in row.reasons),
        decided_at=row.decided_at,
    )
