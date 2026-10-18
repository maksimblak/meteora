"""SQLAlchemy repository implementations."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from src.core.clock import Clock
from src.domain.models.events import AuditEvent
from src.persistence.contracts import Lease
from src.persistence.models import AuditEventRecord, OutboxEventRecord, SystemLeaseRecord


class SqlAlchemyRepository[ModelT: DeclarativeBase]:
    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    async def add(self, record: ModelT) -> None:
        self._session.add(record)

    async def get(self, record_id: UUID) -> ModelT | None:
        return await self._session.get(self._model, record_id)


class SqlAlchemyLeaseRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def try_acquire(self, name: str, owner: str, ttl: timedelta) -> Lease | None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        now = self._clock.now()
        expires_at = now + ttl
        statement = (
            insert(SystemLeaseRecord)
            .values(name=name, owner=owner, acquired_at=now, expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=[SystemLeaseRecord.name],
                set_={"owner": owner, "acquired_at": now, "expires_at": expires_at},
                where=SystemLeaseRecord.expires_at <= now,
            )
            .returning(SystemLeaseRecord)
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        if record is None:
            return None
        return Lease(record.name, record.owner, record.acquired_at, record.expires_at)


def _event_values(event: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at,
        "aggregate_id": event.aggregate_id,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type.value,
        "payload": event.payload,
    }


class SqlAlchemyAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> None:
        self._session.add(AuditEventRecord(**_event_values(event)))


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> None:
        self._session.add(OutboxEventRecord(**_event_values(event), publish_attempts=0))
