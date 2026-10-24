"""Repository factory shared by the API and the worker (D01).

* ``DATABASE_URL`` configured -> every store is PostgreSQL on one shared database
  (one connection pool per process).
* No database URL -> ``LOCAL_SQLITE_PATH`` selects the T04/T05 SQLite adapters with
  process-local coordination, *only* outside ``production``; it exists for tests
  and local debugging and is never a multi-process deployment.
* Neither -> refuse to start: there is no silent in-memory fallback.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from src.accounting.models import LedgerEntry
from src.accounting.repository import AccountingRepository, SqliteAccountingRepository
from src.core.clock import Clock, SystemClock
from src.core.config import AppConfig
from src.domain.models.events import AuditEvent
from src.execution.contracts import ExecutionPhase
from src.execution.service import ExecutionRecord, ExecutionStore
from src.market.contracts import UniverseSnapshotStore
from src.market.universe import InMemoryUniverseStore
from src.modes.models import CriticalAlert, KillSwitchPositionAction, ModeTransition, PendingPlan
from src.modes.repository import ModeRepository, SqliteModeRepository
from src.persistence.contracts import (
    AuditLog,
    DecisionJournal,
    DecisionStore,
    EventSink,
    JobLeases,
    PositionContextStore,
    SnapshotHistory,
)
from src.persistence.memory import (
    InMemoryAuditLog,
    InMemoryDecisionStore,
    InMemoryJobLeases,
    InMemoryRecoverableExecutionStore,
    InMemorySnapshotHistory,
)
from src.persistence.outbox import (
    PostgresOutboxRelay,
    PostgresRetention,
    RelayReport,
    RetentionPolicy,
    RetentionReport,
)
from src.persistence.postgres import PostgresDatabase
from src.persistence.sqlite_stores import SqlitePositionContexts
from src.portfolio.models import PositionOutcome, ShadowPosition

LOCAL_SQLITE_PATH = "LOCAL_SQLITE_PATH"


class PersistenceBackend(StrEnum):
    POSTGRESQL = "POSTGRESQL"
    SQLITE = "SQLITE"


class PersistenceConfigurationError(RuntimeError):
    pass


class ModeStore(ModeRepository, Protocol):
    """T04 mode contract plus the read views the API and recovery need."""

    def entry_plan(self, plan_id: UUID) -> PendingPlan | None: ...

    def entry_plans(self) -> tuple[PendingPlan, ...]: ...

    def pending_count(self) -> int: ...

    def transitions(self) -> tuple[ModeTransition, ...]: ...

    def alerts(self) -> tuple[CriticalAlert, ...]: ...

    def position_actions(self) -> tuple[KillSwitchPositionAction, ...]: ...

    def outbox_events(self) -> tuple[AuditEvent, ...]: ...

    def close(self) -> None: ...


class AccountingStore(AccountingRepository, Protocol):
    def positions(self) -> tuple[ShadowPosition, ...]: ...

    def ledger_entries(self) -> tuple[LedgerEntry, ...]: ...

    def outcomes(self) -> tuple[PositionOutcome, ...]: ...

    def outbox_events(self) -> tuple[AuditEvent, ...]: ...

    def close(self) -> None: ...


class ExecutionStateStore(ExecutionStore, Protocol):
    async def in_phases(self, phases: Sequence[ExecutionPhase]) -> tuple[ExecutionRecord, ...]: ...


class OutboxRelay(Protocol):
    async def publish_pending(self) -> RelayReport: ...

    async def pending_count(self) -> int: ...


class Retention(Protocol):
    async def purge(self, now: datetime) -> RetentionReport: ...


@dataclass
class Repositories:
    backend: PersistenceBackend
    mode: ModeStore
    accounting: AccountingStore
    execution: ExecutionStateStore
    leases: JobLeases
    audit: AuditLog
    universe: UniverseSnapshotStore
    history: SnapshotHistory
    decisions: DecisionStore
    positions: PositionContextStore
    database: PostgresDatabase | None = None
    closers: tuple[Callable[[], None], ...] = ()
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    _closed: bool = field(default=False, repr=False)

    @property
    def journal(self) -> DecisionJournal:
        """The read side of the decision records (pages, plans, standing decisions)."""

        if isinstance(self.decisions, DecisionJournal):
            return self.decisions
        raise PersistenceConfigurationError("this decision store offers no decision journal")

    def outbox_relay(
        self, sink: EventSink, clock: Clock, *, owner: str, batch_size: int = 500
    ) -> OutboxRelay | None:
        if self.database is None:
            return None
        return PostgresOutboxRelay(self.database, sink, clock, owner=owner, batch_size=batch_size)

    def retention(self, published_outbox_age: timedelta | None = None) -> Retention | None:
        if self.database is None:
            return None
        policy = (
            self.retention_policy
            if published_outbox_age is None
            else replace(self.retention_policy, published_outbox_age=published_outbox_age)
        )
        return PostgresRetention(self.database, policy)

    async def ping(self) -> bool:
        if self.database is None:
            try:
                self.mode.mode_state()
            except Exception:
                return False
            return True
        return await self.database.ping()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.database is not None:
            self.database.close()
        for closer in self.closers:
            closer()


def open_repositories(
    config: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    schema: str | None = None,
    pool_size: int = 5,
) -> Repositories:
    clock = clock or SystemClock()
    dsn = config.database_dsn()
    if dsn:
        repositories = _postgres(dsn, clock, schema=schema, pool_size=pool_size)
        try:
            repositories.retention_policy = RetentionPolicy.from_environment(environ or {})
        except BaseException:
            repositories.close()
            raise
        return repositories
    sqlite_path = (environ or {}).get(LOCAL_SQLITE_PATH)
    if not sqlite_path:
        raise PersistenceConfigurationError(
            "DATABASE_URL (or DATABASE_URL_FILE) is required; SQLite is only for local debugging"
        )
    if config.app_env == "production":
        raise PersistenceConfigurationError(
            "SQLite persistence is not allowed in production; configure DATABASE_URL"
        )
    return _sqlite(sqlite_path, clock)


def _postgres(dsn: str, clock: Clock, *, schema: str | None, pool_size: int) -> Repositories:
    from src.persistence.accounting_repository import PostgresAccountingRepository
    from src.persistence.decisions import PostgresDecisionStore
    from src.persistence.execution_store import PostgresExecutionStore
    from src.persistence.leases import PostgresJobLeases
    from src.persistence.market_store import PostgresPositionContexts, PostgresUniverseStore
    from src.persistence.mode_repository import PostgresModeRepository
    from src.persistence.outbox import PostgresAuditLog

    database = PostgresDatabase(dsn, schema=schema, pool_size=pool_size)
    try:
        universe = PostgresUniverseStore(database)
        return Repositories(
            backend=PersistenceBackend.POSTGRESQL,
            mode=PostgresModeRepository(database),
            accounting=PostgresAccountingRepository(database),
            execution=PostgresExecutionStore(database),
            leases=PostgresJobLeases(database, clock),
            audit=PostgresAuditLog(database),
            universe=universe,
            history=universe,
            decisions=PostgresDecisionStore(database),
            positions=PostgresPositionContexts(database),
            database=database,
        )
    except BaseException:
        database.close()
        raise


def _sqlite(path: str, clock: Clock) -> Repositories:
    universe = InMemoryUniverseStore()
    mode = SqliteModeRepository(path)
    accounting = SqliteAccountingRepository(path)
    contexts = SqlitePositionContexts(path)
    return Repositories(
        backend=PersistenceBackend.SQLITE,
        mode=mode,
        accounting=accounting,
        execution=InMemoryRecoverableExecutionStore(),
        leases=InMemoryJobLeases(clock),
        audit=InMemoryAuditLog(),
        universe=universe,
        history=InMemorySnapshotHistory(universe),
        decisions=InMemoryDecisionStore(),
        positions=contexts,
        closers=(mode.close, accounting.close, contexts.close),
    )
