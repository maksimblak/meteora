"""Transactional event recording shared by every PostgreSQL adapter.

Each event is written twice inside the caller's transaction: to the append-only
``audit_events`` trail (never purged) and to ``outbox_events`` (the publish queue,
eligible for retention once published). Both writes go through the T01 repositories.
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.events import AuditEvent, EventType
from src.persistence.models import AuditEventRecord
from src.persistence.repositories import SqlAlchemyAuditRepository, SqlAlchemyOutboxRepository


async def append_event(session: AsyncSession, event: AuditEvent) -> None:
    await SqlAlchemyAuditRepository(session).append(event)
    await SqlAlchemyOutboxRepository(session).append(event)
    await session.flush()


def event_from_row(row: Any) -> AuditEvent:
    return AuditEvent(
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        aggregate_id=row.aggregate_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        schema_version=row.schema_version,
        event_type=EventType(row.event_type),
        payload=row.payload,
    )


async def events_of_types(
    session: AsyncSession, types: Iterable[EventType]
) -> tuple[AuditEvent, ...]:
    """Audit-trail view of the given event types in commit order."""

    rows = await session.execute(
        select(
            AuditEventRecord.event_id,
            AuditEventRecord.occurred_at,
            AuditEventRecord.aggregate_id,
            AuditEventRecord.correlation_id,
            AuditEventRecord.causation_id,
            AuditEventRecord.schema_version,
            AuditEventRecord.event_type,
            AuditEventRecord.payload,
        )
        .where(AuditEventRecord.event_type.in_([kind.value for kind in types]))
        .order_by(AuditEventRecord.seq)
    )
    return tuple(event_from_row(row) for row in rows)
