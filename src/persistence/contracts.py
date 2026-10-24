"""Provider-neutral persistence contracts exposed to the application layer."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self, TypeVar, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.domain.models.events import AuditEvent
from src.domain.models.trading import TradingMode
from src.domain.risk.models import SizedPlan
from src.domain.screening.models import CandidateDecision, CandidateStatus, ScreeningPolicy
from src.execution.contracts import ExecutionPhase
from src.execution.service import ExecutionRecord
from src.market.models import Freshness, PoolSnapshot, TokenRiskSnapshot

RecordT = TypeVar("RecordT")


class Repository(Protocol[RecordT]):
    async def add(self, record: RecordT) -> None: ...

    async def get(self, record_id: UUID) -> RecordT | None: ...


@dataclass(frozen=True, slots=True)
class Lease:
    name: str
    owner: str
    acquired_at: datetime
    expires_at: datetime


class LeaseRepository(Protocol):
    async def try_acquire(self, name: str, owner: str, ttl: timedelta) -> Lease | None: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class OutboxRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class UnitOfWork(Protocol):
    pools: Repository[object]
    snapshots: Repository[object]
    decisions: Repository[object]
    plans: Repository[object]
    positions: Repository[object]
    executions: Repository[object]
    modes: Repository[object]
    risk_days: Repository[object]
    alerts: Repository[object]
    leases: LeaseRepository
    audit: AuditRepository
    outbox: OutboxRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


# --- worker coordination (T07) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobLease:
    """Exclusive right to run one named job until ``expires_at``.

    ``token`` is a fencing number that grows on every acquisition, so a stale owner
    can never renew or release a lease that was taken over after it expired.
    """

    name: str
    owner: str
    token: int
    acquired_at: datetime
    expires_at: datetime


class JobLeases(Protocol):
    async def acquire(self, name: str, owner: str, ttl: timedelta) -> JobLease | None: ...

    async def renew(self, lease: JobLease, ttl: timedelta) -> JobLease | None: ...

    async def release(self, lease: JobLease) -> None: ...


class AuditLog(Protocol):
    """Durable, append-only audit trail; a failed append must raise, never drop."""

    async def append(self, event: AuditEvent) -> None: ...


class EventSink(Protocol):
    """External publication target; must treat a repeated ``event_id`` as a no-op."""

    async def publish(self, event: AuditEvent) -> None: ...


# --- market history and decision records (R17, R29i) -----------------------------------


class SnapshotHistory(Protocol):
    async def history(
        self, pool_address: str, *, until: datetime, limit: int
    ) -> tuple[PoolSnapshot, ...]: ...

    async def latest_universe(self) -> tuple[PoolSnapshot, ...]: ...

    async def latest_sync_at(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class UniverseSummary:
    """What a status page needs of the latest universe, without loading every snapshot."""

    observed_at: datetime | None
    addresses: frozenset[str]
    # Some pool carries an active bin and both token decimals (D06): entries can be built.
    anchored: bool


@runtime_checkable
class UniverseSummaryReader(Protocol):
    async def latest_universe_summary(self) -> UniverseSummary: ...


def summarize_universe(
    snapshots: Sequence[PoolSnapshot], observed_at: datetime | None
) -> UniverseSummary:
    return UniverseSummary(
        observed_at=observed_at,
        addresses=frozenset(item.pool.address.root for item in snapshots),
        anchored=any(
            item.active_bin_id is not None
            and item.base_decimals is not None
            and item.quote_decimals is not None
            for item in snapshots
        ),
    )


async def universe_summary(history: SnapshotHistory) -> UniverseSummary:
    """One light read where the store offers it; otherwise derived from the snapshots."""

    if isinstance(history, UniverseSummaryReader):
        return await history.latest_universe_summary()
    return summarize_universe(await history.latest_universe(), await history.latest_sync_at())


class DecisionFacts(BaseModel):
    """The market facts an operator needs to understand a decision at a glance."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    pool_address: str
    volume_short: Decimal | None
    volume_long: Decimal | None
    volume_24h: Decimal | None
    tvl: Decimal
    fee_tvl: Decimal | None
    price: Decimal
    price_volatility: Decimal | None
    pool_age_seconds: int
    freshness: Freshness
    source_age_seconds: int
    observed_at: datetime


class DecisionRecord(BaseModel):
    """Immutable decision record: inputs, features, thresholds, reasons and versions."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_id: UUID
    decision: CandidateDecision
    facts: DecisionFacts
    market: PoolSnapshot
    token: TokenRiskSnapshot
    thresholds: ScreeningPolicy
    recorded_at: datetime


class PlanRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plan_id: UUID
    decision_id: UUID
    sized_plan: SizedPlan
    admitted: bool
    admission_reasons: tuple[str, ...]
    mode: TradingMode
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """The decision that stands for a pool after an evaluation.

    ``recorded`` is ``False`` when the pool's outcome had not changed: no new record was
    written and ``decision_id`` names the earlier one, which still holds (R29i.2).
    """

    decision_id: UUID
    recorded: bool


def outcome_key(decision: CandidateDecision) -> str:
    """What makes two decisions of a pool "the same outcome": status, reasons, flags,
    missing signals, mode and the rule/config versions - not the score or the facts."""

    material = {
        "status": decision.status.value,
        "reasons": [reason.value for reason in decision.reasons],
        "flags": [flag.value for flag in decision.flags],
        "missing_signals": list(decision.missing_signals),
        "mode": decision.mode.value,
        "ruleset_version": decision.ruleset_version,
        "config_version": decision.config_version,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DecisionStore(Protocol):
    async def record_decision(
        self, record: DecisionRecord, *, evaluated_at: datetime | None = None
    ) -> UUID:
        """Write the record whatever the pool's previous outcome (an accepted candidate:
        its plan needs the facts of this very evaluation). ``evaluated_at`` is the cycle
        instant (the decision's own instant by default)."""
        ...

    async def record_evaluation(
        self, record: DecisionRecord, *, evaluated_at: datetime
    ) -> RecordedDecision:
        """Write the record only if the pool's outcome changed; either way the pool counts
        as evaluated in the cycle of ``evaluated_at``."""
        ...

    async def record_plan(self, record: PlanRecord) -> UUID: ...

    async def decision(self, decision_id: UUID) -> DecisionRecord | None: ...

    async def plan(self, plan_id: UUID) -> PlanRecord | None: ...


# --- decision journal (read side of the operator panel, C5) ------------------------------


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


@runtime_checkable
class DecisionJournal(Protocol):
    """Newest-first pages, the plan of a decision, the decision standing for each pool and
    the outcomes of the current cycle. Tables, SQL and indexes stay behind it."""

    async def page(self, query: DecisionQuery) -> DecisionPage: ...

    async def plan_for(self, decision_id: UUID) -> PlanRecord | None: ...

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]: ...

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]:
        """Per pool the standing outcome, only for pools evaluated in the last two candidate
        cycles: the running one and the last complete one."""
        ...


class PositionContext(BaseModel):
    """What the position cycle needs that the accounting projection does not carry."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    plan_id: UUID
    decision_id: UUID
    pool_address: str
    base_mint: str
    entry_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    entry_short_volume: Decimal
    baseline_fee_tvl: Decimal
    entry_tvl: Decimal
    maximum_holding_period: timedelta
    opened_at: datetime
    mode: TradingMode
    ruleset_version: str
    correlation_id: str
    wallet_address: str
    max_slippage_bps: int
    min_bin_id: int
    max_bin_id: int


class PositionContextStore(Protocol):
    async def put(self, context: PositionContext) -> None: ...

    async def get(self, position_id: str) -> PositionContext | None: ...

    async def all(self) -> tuple[PositionContext, ...]: ...


class RecoverableExecutionStore(Protocol):
    """Execution records whose outcome is not final, for restart reconciliation."""

    async def in_phases(self, phases: Sequence[ExecutionPhase]) -> tuple[ExecutionRecord, ...]: ...
