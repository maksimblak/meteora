import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.clock import FrozenClock
from src.domain.models.events import AuditEvent, EventType
from src.persistence.database import create_engine, create_session_factory
from src.persistence.models import OutboxEventRecord, PoolRecord
from src.persistence.testing import integration_database_url
from src.persistence.uow import SqlAlchemyUnitOfWork


def _database_url() -> str:
    # Only the test database: these tests write into its shared schema, never the working one.
    value = integration_database_url()
    if not value:
        pytest.skip("PostgreSQL integration URL is not configured")
    return value


def test_uow_persists_audit_and_lease_acquisition_is_exclusive() -> None:
    async def scenario() -> None:
        engine = create_engine(_database_url())
        sessions = create_session_factory(engine)
        clock = FrozenClock(datetime(2026, 10, 18, 8, 0, tzinfo=UTC))
        lease_name = f"worker-{uuid4()}"
        event = AuditEvent(
            event_id=uuid4(),
            occurred_at=clock.now(),
            aggregate_id=uuid4(),
            correlation_id=uuid4(),
            causation_id=None,
            schema_version=1,
            event_type=EventType.MODE_CHANGED,
            payload={"from_mode": "SHADOW", "to_mode": "SHADOW"},
        )

        async with SqlAlchemyUnitOfWork(sessions, clock=clock) as first:
            lease = await first.leases.try_acquire(lease_name, "owner-a", timedelta(seconds=30))
            await first.audit.append(event)
            await first.commit()
        assert lease is not None

        async with SqlAlchemyUnitOfWork(sessions, clock=clock) as second:
            contender = await second.leases.try_acquire(
                lease_name, "owner-b", timedelta(seconds=30)
            )
            await second.rollback()
        assert contender is None

        await engine.dispose()

    asyncio.run(scenario())


def test_generic_repository_and_transactional_outbox_persist_across_sessions() -> None:
    async def scenario() -> None:
        engine = create_engine(_database_url())
        sessions = create_session_factory(engine)
        pool_id = uuid4()
        event_id = uuid4()
        event = AuditEvent(
            event_id=event_id,
            occurred_at=datetime(2026, 10, 18, 8, 0, tzinfo=UTC),
            aggregate_id=pool_id,
            correlation_id=uuid4(),
            causation_id=None,
            schema_version=1,
            event_type=EventType.POOL_SNAPSHOT_RECORDED,
            payload={"pool_id": str(pool_id)},
        )
        pool = PoolRecord(
            id=pool_id,
            address=f"pool-{uuid4()}",
            base_mint="1" * 32,
            quote_mint="2" * 32,
        )

        async with SqlAlchemyUnitOfWork(sessions) as writer:
            await writer.pools.add(pool)
            await writer.outbox.append(event)
            await writer.commit()

        async with SqlAlchemyUnitOfWork(sessions) as reader:
            recovered = await reader.pools.get(pool_id)
            recovered_address = recovered.address if recovered is not None else None
            await reader.rollback()

        async with engine.connect() as connection:
            outbox_count = await connection.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(OutboxEventRecord.event_id == event_id)
            )

        assert recovered_address == pool.address
        assert outbox_count == 1
        await engine.dispose()

    asyncio.run(scenario())
