"""Provider-neutral persistence contracts exposed to the application layer."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Protocol, Self, TypeVar
from uuid import UUID

from src.domain.models.events import AuditEvent

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
