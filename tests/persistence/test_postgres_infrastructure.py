"""PostgreSQL-only guarantees: leases, outbox relay, retention, audit immutability."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from src.core.clock import FrozenClock
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmOperation,
    ExecutionPhase,
    LiquidityStrategy,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import ExecutionBlocked, ExecutionRecord
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics
from src.market.universe import SyncStatus, UniverseSync
from src.persistence.execution_store import PostgresExecutionStore
from src.persistence.leases import PostgresJobLeases
from src.persistence.market_store import PostgresUniverseStore
from src.persistence.memory import InMemoryEventSink
from src.persistence.models import AuditEventRecord, OutboxEventRecord
from src.persistence.outbox import PostgresAuditLog, PostgresOutboxRelay, PostgresRetention
from src.persistence.testing import MigratedSchema
from src.providers.fixtures import FixtureMarketDataProvider

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
KEY = "11111111111111111111111111111111"
POOL_A = "SysvarRent111111111111111111111111111111111"
POOL_B = "So11111111111111111111111111111111111111112"


def event(kind: EventType = EventType.CANDIDATE_EVALUATED) -> AuditEvent:
    return AuditEvent(
        event_id=uuid4(),
        occurred_at=NOW,
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        causation_id=None,
        schema_version=1,
        event_type=kind,
        payload={"note": "fixture"},
    )


def snapshot(address: str, observed_at: datetime, volume: str = "100") -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal(volume), fees=Decimal("1.5"))
        for name in ("5m", "30m", "1h", "4h", "24h")
    }
    return PoolSnapshot(
        pool=PoolRef(address=address, base_mint=KEY, quote_mint=POOL_B),
        windows=windows,
        tvl=Decimal("12345.678901234567890123"),
        depth=Decimal("500"),
        price=Decimal("1.25"),
        bin_step=25,
        created_at=observed_at - timedelta(days=30),
        observed_at=observed_at,
        source_at=observed_at,
        ingested_at=observed_at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def scalar(schema: MigratedSchema, statement: Any) -> Any:
    database = schema.connect()

    async def work(session: Any) -> Any:
        return await session.scalar(statement)

    try:
        return database.run(work)
    finally:
        database.close()


# --- leases ---------------------------------------------------------------------------


def test_lease_excludes_a_second_worker_and_recovers_after_owner_crash(
    postgres_schema: MigratedSchema,
) -> None:
    async def scenario() -> None:
        clock = FrozenClock(NOW)
        first_db, second_db = postgres_schema.connect(), postgres_schema.connect()
        first, second = PostgresJobLeases(first_db, clock), PostgresJobLeases(second_db, clock)
        try:
            held = await first.acquire("universe_sync", "worker-a", timedelta(seconds=30))
            assert held is not None
            assert await second.acquire("universe_sync", "worker-b", timedelta(seconds=30)) is None

            # Owner crashes: its connection (advisory lock) is gone, the row is not.
            await first.abandon()
            assert await second.acquire("universe_sync", "worker-b", timedelta(seconds=30)) is None
            clock.advance(timedelta(seconds=30))
            taken = await second.acquire("universe_sync", "worker-b", timedelta(seconds=30))
            assert taken is not None
            assert taken.owner == "worker-b" and taken.token > held.token

            # The stale owner can neither renew nor release the taken-over lease.
            assert await first.renew(held, timedelta(seconds=30)) is None
            await first.release(held)
            assert await second.holder("universe_sync") == taken

            renewed = await second.renew(taken, timedelta(minutes=1))
            assert renewed is not None and renewed.expires_at == NOW + timedelta(seconds=90)
            await second.release(taken)
            assert await second.holder("universe_sync") is None
            again = await first.acquire("universe_sync", "worker-a", timedelta(seconds=30))
            assert again is not None and again.token > taken.token
            await first.release(again)
        finally:
            first_db.close()
            second_db.close()

    asyncio.run(scenario())


def test_concurrent_acquirers_get_exactly_one_lease(postgres_schema: MigratedSchema) -> None:
    async def scenario() -> None:
        clock = FrozenClock(NOW)
        databases = [postgres_schema.connect() for _ in range(4)]
        leases = [PostgresJobLeases(database, clock) for database in databases]
        try:
            results = await asyncio.gather(
                *(
                    store.acquire("position_cycle", f"worker-{index}", timedelta(seconds=30))
                    for index, store in enumerate(leases)
                )
            )
            winners = [
                (store, lease)
                for store, lease in zip(leases, results, strict=True)
                if lease is not None
            ]
            assert len(winners) == 1
            await winners[0][0].release(winners[0][1])
        finally:
            for database in databases:
                database.close()

    asyncio.run(scenario())


# --- audit, outbox and retention -------------------------------------------------------


def test_outbox_relay_is_idempotent_retries_with_backoff_and_shares_no_rows(
    postgres_schema: MigratedSchema,
) -> None:
    async def scenario() -> None:
        clock = FrozenClock(NOW)
        database = postgres_schema.connect()
        try:
            log = PostgresAuditLog(database)
            events = [event(), event(), event()]
            for item in events:
                await log.append(item)
            await log.append(events[0])  # idempotent re-append

            sink = InMemoryEventSink()
            sink.fail_with = RuntimeError("sink down")
            relay = PostgresOutboxRelay(database, sink, clock, owner="relay-a", batch_size=2)
            failed = await relay.publish_pending()
            assert (failed.claimed, failed.published, failed.failed) == (2, 0, 2)

            sink.fail_with = None
            # The failed rows back off; only the third, never-tried event is due now.
            first = await relay.publish_pending()
            assert (first.claimed, first.published) == (1, 1)
            clock.advance(timedelta(seconds=5))
            other = PostgresOutboxRelay(database, sink, clock, owner="relay-b", batch_size=10)
            second = await other.publish_pending()
            assert (second.claimed, second.published) == (2, 2)
            assert (await relay.publish_pending()).claimed == 0
            assert set(sink.published) == {item.event_id for item in events}
            assert await relay.pending_count() == 0
        finally:
            database.close()

    asyncio.run(scenario())
    attempts = scalar(
        postgres_schema,
        select(func.max(OutboxEventRecord.publish_attempts)),
    )
    assert attempts == 1


def test_retention_purges_published_telemetry_but_never_the_audit_trail(
    postgres_schema: MigratedSchema,
) -> None:
    async def scenario() -> None:
        clock = FrozenClock(NOW)
        database = postgres_schema.connect()
        try:
            log = PostgresAuditLog(database)
            for _ in range(3):
                await log.append(event())
            relay = PostgresOutboxRelay(
                database, InMemoryEventSink(), clock, owner="relay", batch_size=2
            )
            await relay.publish_pending()
            await log.append(event())  # stays unpublished

            report = await PostgresRetention(database).purge(NOW + timedelta(days=8))
            assert report.outbox_deleted == 2
        finally:
            database.close()

    asyncio.run(scenario())
    assert scalar(postgres_schema, select(func.count()).select_from(AuditEventRecord)) == 4
    assert scalar(postgres_schema, select(func.count()).select_from(OutboxEventRecord)) == 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET payload = '{}'::jsonb",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_audit_trail_is_append_only_at_the_database_level(
    postgres_schema: MigratedSchema, statement: str
) -> None:
    database = postgres_schema.connect()
    try:
        asyncio.run(PostgresAuditLog(database).append(event()))

        async def mutate(session: Any) -> None:
            await session.execute(text(statement))

        with pytest.raises(DBAPIError, match="append-only"):
            database.run(mutate)
    finally:
        database.close()
    assert scalar(postgres_schema, select(func.count()).select_from(AuditEventRecord)) == 1


def test_migrations_downgrade_and_upgrade_cleanly(postgres_schema: MigratedSchema) -> None:
    postgres_schema.alembic("downgrade", "0001_foundation")
    postgres_schema.alembic("upgrade", "head")
    assert scalar(postgres_schema, text("SELECT version_num FROM alembic_version")) == (
        "0004_decision_journal"
    )


# --- universe history -------------------------------------------------------------------


def test_universe_sync_publishes_whole_versions_and_keeps_exact_history(
    postgres_schema: MigratedSchema,
) -> None:
    from src.market.models import PoolPage

    async def scenario() -> None:
        database = postgres_schema.connect()
        store = PostgresUniverseStore(database)
        clock = FrozenClock(NOW)
        try:
            pools = (
                PoolRef(address=POOL_A, base_mint=KEY, quote_mint=POOL_B),
                PoolRef(address=POOL_B, base_mint=KEY, quote_mint=POOL_B),
            )
            provider = FixtureMarketDataProvider(
                pages=[PoolPage(items=pools)],
                snapshots=[snapshot(POOL_A, NOW), snapshot(POOL_B, NOW)],
            )
            first = await UniverseSync(provider, store, clock=clock).run(uuid4())
            assert first.status is SyncStatus.COMPLETED and first.pool_count == 2
            replay = await UniverseSync(provider, store, clock=clock).run(uuid4())
            assert replay.duplicate_count == 2

            broken = FixtureMarketDataProvider(
                pages=[PoolPage(items=pools)], snapshots=[snapshot(POOL_A, NOW)]
            )
            clock.advance(timedelta(minutes=5))
            failed = await UniverseSync(broken, store, clock=clock).run(uuid4())
            assert failed.status is SyncStatus.FAILED
            assert await store.latest_sync_at() == NOW

            history = await store.history(POOL_A, until=NOW, limit=10)
            assert history == (snapshot(POOL_A, NOW),)
            assert history[0].tvl == Decimal("12345.678901234567890123")
            universe = await store.latest_universe()
            assert {item.pool.address.root for item in universe} == {POOL_A, POOL_B}
        finally:
            database.close()

    asyncio.run(scenario())


# --- execution store -----------------------------------------------------------------------


def _execution_record(phase: ExecutionPhase, key: str) -> ExecutionRecord:
    command = DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="rules-v1",
        mode=TradingMode.LIVE,
        operation=DlmmOperation.OPEN,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x="10",
        amount_y="10",
    )
    simulation = bind_simulation_result(
        command_id=command.command_id,
        command_digest=canonical_command_digest(command),
        success=True,
        simulated_at=NOW,
        blockhash="hash",
        last_valid_block_height=1,
        compute_units=1,
        network_fee_lamports="5000",
        priority_fee_lamports="0",
        price_impact_bps=10,
        min_bin_id=-2,
        max_bin_id=2,
        errors=(),
    )
    return ExecutionRecord(command=command, simulation=simulation, phase=phase)


def test_execution_store_enforces_idempotency_and_lists_unresolved_commands(
    postgres_schema: MigratedSchema,
) -> None:
    async def scenario() -> None:
        database = postgres_schema.connect()
        restarted_db = postgres_schema.connect()
        try:
            store = PostgresExecutionStore(database)
            unknown = _execution_record(ExecutionPhase.UNKNOWN, "entry:unknown")
            done = _execution_record(ExecutionPhase.CONFIRMED, "entry:done")
            await store.put(unknown)
            await store.put(done)
            await store.put(unknown)
            stolen = _execution_record(ExecutionPhase.SIMULATED, "entry:unknown")
            with pytest.raises(ExecutionBlocked, match="idempotency"):
                await store.put(stolen)
            assert await store.claim_approval(uuid4(), "a" * 64, "b" * 64) is True

            restarted = PostgresExecutionStore(restarted_db)
            pending = await restarted.in_phases(
                (ExecutionPhase.SUBMITTING, ExecutionPhase.UNKNOWN, ExecutionPhase.SUBMITTED)
            )
            assert [record.command.command_id for record in pending] == [unknown.command.command_id]
            loaded = await restarted.get_by_idempotency_key("entry:done")
            assert loaded is not None and loaded.command == done.command
        finally:
            database.close()
            restarted_db.close()

    asyncio.run(scenario())
