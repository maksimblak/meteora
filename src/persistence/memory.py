"""Process-local implementations of the T07 persistence ports.

They follow the PostgreSQL semantics (exclusive leases with fencing, idempotent
records, append-only audit) and serve as test seams and the SQLite local-debug
backend. They are never selected by the factory for API/worker production use.
"""

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from src.core.clock import Clock
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.primitives import PublicKey
from src.execution.contracts import ExecutionPhase, canonical_command_digest
from src.execution.service import ExecutionBlocked, ExecutionRecord
from src.market.models import PoolSnapshot
from src.market.universe import InMemoryUniverseStore
from src.persistence.contracts import (
    DecisionOutcome,
    DecisionPage,
    DecisionQuery,
    DecisionRecord,
    JobLease,
    PlanRecord,
    PositionContext,
    RecordedDecision,
    outcome_key,
)


class InMemoryJobLeases:
    """Shared by several schedulers in one test to model several worker processes."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._leases: dict[str, JobLease] = {}
        self._tokens: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, name: str, owner: str, ttl: timedelta) -> JobLease | None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        async with self._lock:
            now = self._clock.now()
            current = self._leases.get(name)
            if current is not None and current.expires_at > now and current.owner != owner:
                return None
            token = self._tokens.get(name, 0) + 1
            self._tokens[name] = token
            lease = JobLease(name, owner, token, now, now + ttl)
            self._leases[name] = lease
            return lease

    async def renew(self, lease: JobLease, ttl: timedelta) -> JobLease | None:
        async with self._lock:
            now = self._clock.now()
            current = self._leases.get(lease.name)
            if current is None or current.token != lease.token or current.owner != lease.owner:
                return None
            if current.expires_at <= now:
                return None
            renewed = replace(current, expires_at=now + ttl)
            self._leases[lease.name] = renewed
            return renewed

    async def release(self, lease: JobLease) -> None:
        async with self._lock:
            current = self._leases.get(lease.name)
            if current is not None and current.token == lease.token:
                del self._leases[lease.name]

    def holder(self, name: str) -> JobLease | None:
        return self._leases.get(name)


class InMemoryAuditLog:
    """Append-only list; ``fail_with`` makes appends raise (outage injection).

    ``fail_on`` narrows the outage to the listed event types.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.fail_with: Exception | None = None
        self.fail_on: frozenset[EventType] | None = None

    async def append(self, event: AuditEvent) -> None:
        if self.fail_with is not None and (
            self.fail_on is None or event.event_type in self.fail_on
        ):
            raise self.fail_with
        if any(existing.event_id == event.event_id for existing in self.events):
            return
        self.events.append(event)


class InMemoryEventSink:
    """Idempotent publication target that records each distinct event once."""

    def __init__(self) -> None:
        self.published: dict[UUID, AuditEvent] = {}
        self.deliveries = 0
        self.fail_with: Exception | None = None

    async def publish(self, event: AuditEvent) -> None:
        self.deliveries += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.published.setdefault(event.event_id, event)


class InMemorySnapshotHistory:
    """History view over the T02 in-memory universe store."""

    def __init__(self, store: InMemoryUniverseStore) -> None:
        self._store = store

    async def history(
        self, pool_address: str, *, until: datetime, limit: int
    ) -> tuple[PoolSnapshot, ...]:
        items = [
            item
            for item in self._store.history_for(PublicKey(pool_address))
            if item.observed_at <= until
        ]
        return tuple(items[-limit:])

    async def latest_universe(self) -> tuple[PoolSnapshot, ...]:
        current = self._store.current
        return () if current is None else current.snapshots

    async def latest_sync_at(self) -> datetime | None:
        current = self._store.current
        return None if current is None else current.observed_at


@dataclass(frozen=True, slots=True)
class _Head:
    """The decision standing for a pool, the last cycle that confirmed it and the cycle
    before that (so the journal knows the previous cycle even of an unchanged pool)."""

    decision_id: UUID
    outcome: str
    evaluated_at: datetime
    previous_evaluated_at: datetime | None = None

    def evaluated(self, at: datetime) -> "_Head":
        if at <= self.evaluated_at:
            return self
        return replace(self, evaluated_at=at, previous_evaluated_at=self.evaluated_at)


class InMemoryDecisionStore:
    """``DecisionStore`` and ``DecisionJournal`` with the PostgreSQL semantics: records in
    insertion order (the page cursor is an offset), one head per pool."""

    def __init__(self) -> None:
        self.decisions: dict[UUID, DecisionRecord] = {}
        self.plans: dict[UUID, PlanRecord] = {}
        self.heads: dict[str, _Head] = {}
        self.fail_with: Exception | None = None

    async def record_decision(
        self, record: DecisionRecord, *, evaluated_at: datetime | None = None
    ) -> UUID:
        cycle = evaluated_at or record.decision.decision_at
        return (await self._record(record, cycle, force=True)).decision_id

    async def record_evaluation(
        self, record: DecisionRecord, *, evaluated_at: datetime
    ) -> RecordedDecision:
        return await self._record(record, evaluated_at, force=False)

    async def _record(
        self, record: DecisionRecord, evaluated_at: datetime, *, force: bool
    ) -> RecordedDecision:
        if self.fail_with is not None:
            raise self.fail_with
        address = record.decision.pool_address.root
        key = outcome_key(record.decision)
        head = self.heads.get(address)
        if not force and head is not None and head.outcome == key:
            self.heads[address] = head.evaluated(evaluated_at)
            return RecordedDecision(head.decision_id, recorded=False)
        self.decisions.setdefault(record.decision_id, record)
        previous = None if head is None else head.evaluated(evaluated_at).previous_evaluated_at
        self.heads[address] = _Head(record.decision_id, key, evaluated_at, previous)
        return RecordedDecision(record.decision_id, recorded=True)

    async def record_plan(self, record: PlanRecord) -> UUID:
        if self.fail_with is not None:
            raise self.fail_with
        self.plans.setdefault(record.plan_id, record)
        return record.plan_id

    async def decision(self, decision_id: UUID) -> DecisionRecord | None:
        return self.decisions.get(decision_id)

    async def plan(self, plan_id: UUID) -> PlanRecord | None:
        return self.plans.get(plan_id)

    # --- DecisionJournal ------------------------------------------------------------------

    async def page(self, query: DecisionQuery) -> DecisionPage:
        records = [
            record
            for record in reversed(tuple(self.decisions.values()))
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
        plans = [plan for plan in self.plans.values() if plan.decision_id == decision_id]
        return plans[-1] if plans else None

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]:
        latest: dict[str, DecisionRecord] = {}
        for address in addresses:
            head = self.heads.get(address)
            record = None if head is None else self.decisions.get(head.decision_id)
            if record is not None:
                latest[address] = record
        return latest

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]:
        floor = recent_cycle_floor(
            (head.evaluated_at, head.previous_evaluated_at) for head in self.heads.values()
        )
        outcomes = []
        for address, head in self.heads.items():
            record = self.decisions.get(head.decision_id)
            if record is None or floor is None or head.evaluated_at < floor:
                continue
            outcomes.append(
                DecisionOutcome(
                    pool_address=address,
                    status=record.decision.status,
                    reasons=tuple(reason.value for reason in record.decision.reasons),
                    decided_at=record.decision.decision_at,
                )
            )
        return tuple(outcomes)


def recent_cycle_floor(
    heads: Iterable[tuple[datetime, datetime | None]],
) -> datetime | None:
    """The instant of the last complete candidate cycle: the newest cycle any pool saw
    before the latest one (a cycle in progress has evaluated only part of the pools), or
    the latest one when there was none before it."""

    pairs = list(heads)
    if not pairs:
        return None
    latest = max(evaluated for evaluated, _ in pairs)
    earlier = [
        moment
        for pair in pairs
        for moment in pair
        if moment is not None and moment < latest
    ]
    return max(earlier, default=latest)


class InMemoryPositionContexts:
    def __init__(self) -> None:
        self._contexts: dict[str, PositionContext] = {}

    async def put(self, context: PositionContext) -> None:
        existing = self._contexts.get(context.position_id)
        if existing is not None and existing != context:
            raise ValueError("position context is immutable")
        self._contexts[context.position_id] = context

    async def get(self, position_id: str) -> PositionContext | None:
        return self._contexts.get(position_id)

    async def all(self) -> tuple[PositionContext, ...]:
        return tuple(self._contexts[key] for key in sorted(self._contexts))


class InMemoryRecoverableExecutionStore:
    """T05 execution-store semantics plus the phase listing restart recovery needs."""

    def __init__(self) -> None:
        self._records: dict[UUID, ExecutionRecord] = {}
        self._keys: dict[str, UUID] = {}
        self._claims: set[UUID] = set()

    async def get(self, command_id: UUID) -> ExecutionRecord | None:
        return self._records.get(command_id)

    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        command_id = self._keys.get(key)
        return None if command_id is None else self._records.get(command_id)

    async def put(self, record: ExecutionRecord) -> None:
        existing = self._records.get(record.command.command_id)
        if existing is not None and (
            existing.command.idempotency_key != record.command.idempotency_key
            or canonical_command_digest(existing.command)
            != canonical_command_digest(record.command)
        ):
            raise ExecutionBlocked("command id belongs to another command payload")
        bound = self._keys.get(record.command.idempotency_key)
        if bound is not None and bound != record.command.command_id:
            raise ExecutionBlocked("idempotency key belongs to another command")
        self._records[record.command.command_id] = record
        self._keys[record.command.idempotency_key] = record.command.command_id

    async def claim_approval(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> bool:
        del command_digest, simulation_digest
        if approval_id in self._claims:
            return False
        self._claims.add(approval_id)
        return True

    async def in_phases(self, phases: Sequence[ExecutionPhase]) -> tuple[ExecutionRecord, ...]:
        wanted = set(phases)
        return tuple(record for record in self._records.values() if record.phase in wanted)
