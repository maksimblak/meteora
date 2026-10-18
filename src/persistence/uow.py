"""Async unit-of-work boundary for application transactions."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.persistence.models import (
    AlertRecord,
    CandidateDecisionRecord,
    ExecutionCommandRecord,
    ModeTransitionRecord,
    PoolRecord,
    PoolSnapshotRecord,
    PositionRecord,
    RiskDayRecord,
    StrategyPlanRecord,
)
from src.persistence.repositories import (
    SqlAlchemyAuditRepository,
    SqlAlchemyLeaseRepository,
    SqlAlchemyOutboxRepository,
    SqlAlchemyRepository,
)


class SqlAlchemyUnitOfWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        session = self._session
        self.pools = SqlAlchemyRepository(session, PoolRecord)
        self.snapshots = SqlAlchemyRepository(session, PoolSnapshotRecord)
        self.decisions = SqlAlchemyRepository(session, CandidateDecisionRecord)
        self.plans = SqlAlchemyRepository(session, StrategyPlanRecord)
        self.positions = SqlAlchemyRepository(session, PositionRecord)
        self.executions = SqlAlchemyRepository(session, ExecutionCommandRecord)
        self.modes = SqlAlchemyRepository(session, ModeTransitionRecord)
        self.risk_days = SqlAlchemyRepository(session, RiskDayRecord)
        self.alerts = SqlAlchemyRepository(session, AlertRecord)
        self.leases = SqlAlchemyLeaseRepository(session, self._clock)
        self.audit = SqlAlchemyAuditRepository(session)
        self.outbox = SqlAlchemyOutboxRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc is not None:
            await self._session.rollback()
        await self._session.close()
        self._session = None

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session

    async def commit(self) -> None:
        await self._require_session().commit()

    async def rollback(self) -> None:
        await self._require_session().rollback()
