"""F3 (C1, C5, R29i.2) on PostgreSQL: storage grows with changes, telemetry history is
bounded by a window, the audit trail is never purged, the decision journal is indexed.

Runs only with ``TEST_DATABASE_URL`` (the Compose ``check`` service); skipped otherwise.
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.market.models import PoolRef, PoolSnapshot
from src.persistence.factory import Repositories, open_repositories
from src.persistence.testing import MigratedSchema
from src.worker.pipeline import CandidatePipeline
from tests.api.support import POOL_B, POOL_C
from tests.worker.harness import NOW, POOL, Harness, snapshot

POOLS = (POOL, POOL_B, POOL_C)
CYCLE = timedelta(seconds=5)
SYNC = timedelta(minutes=5)


def repositories(schema: MigratedSchema, **environ: str) -> Repositories:
    environment = {"DATABASE_URL": schema.database_url, **environ}
    return open_repositories(
        AppConfig.load(environ=environment),
        environ=environment,
        clock=FrozenClock(NOW),
        schema=schema.schema,
    )


def pipeline(harness: Harness, repos: Repositories) -> CandidatePipeline:
    """The harness pipeline with its history, decisions and audit on PostgreSQL."""

    return CandidatePipeline(
        history=repos.history,
        onchain=harness.onchain,
        decisions=repos.decisions,
        modes=harness.modes,
        entries=harness.entries,
        portfolio=harness.portfolio,
        audit=repos.audit,
        alerts=harness.alerts,
        policy=harness.policy,
        clock=harness.clock,
        metrics=harness.metrics,
    )


def pool_snapshot(pool: PoolRef, at: Any, *, tvl: str = "999") -> PoolSnapshot:
    return snapshot(at, tvl=tvl).model_copy(update={"pool": pool})


async def sync(repos: Repositories, at: Any, *, tvl: str = "999") -> None:
    stage = await repos.universe.begin(uuid4(), at)
    await stage.stage(tuple(pool_snapshot(pool, at, tvl=tvl) for pool in POOLS))
    await stage.commit()


def count(repos: Repositories, table: str) -> int:
    assert repos.database is not None

    async def work(session: Any) -> int:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")))  # noqa: S608

    return repos.database.run(work)


@pytest.fixture
def stores(postgres_schema: MigratedSchema) -> Iterator[Repositories]:
    repos = repositories(postgres_schema, HISTORY_RETENTION_HOURS="1",
                         WORKER_UNIVERSE_INTERVAL_SECONDS="1")
    yield repos
    repos.close()


TABLES = ("audit_events", "outbox_events", "candidate_decisions")


def test_ten_cycles_of_an_unchanged_universe_add_at_most_k_rows(stores: Repositories) -> None:
    harness = Harness()
    candidates = pipeline(harness, stores)

    async def scenario() -> None:
        for index in range(7):
            await sync(stores, NOW - SYNC * (6 - index))
        harness.keeper.ensure_open()
        for index in range(10):
            for pool in POOLS:
                await candidates.evaluate(pool.address.root, NOW + CYCLE * index)

    before = {table: count(stores, table) for table in TABLES}
    asyncio.run(scenario())
    after = {table: count(stores, table) for table in TABLES}

    for table in TABLES:
        assert after[table] - before[table] <= len(POOLS), table


def test_retention_bounds_telemetry_history_and_keeps_the_audit_trail(
    stores: Repositories,
) -> None:
    harness = Harness()
    candidates = pipeline(harness, stores)

    async def scenario() -> None:
        for index in range(7):
            await sync(stores, NOW - SYNC * (6 - index))
        harness.keeper.ensure_open()
        for pool in POOLS:
            await candidates.evaluate(pool.address.root, NOW)

    asyncio.run(scenario())
    audit_before = count(stores, "audit_events")
    later = NOW + timedelta(hours=3)
    retention = stores.retention()
    assert retention is not None

    asyncio.run(retention.purge(later))

    # Older than the 1 h window: only the newest universe version and each pool's newest
    # snapshot stay (the universe and position valuation read them); decisions without a
    # plan, their feature vectors and the pools' stale heads go; the audit trail stays.
    assert count(stores, "universe_syncs") == 1
    assert count(stores, "universe_members") == len(POOLS)
    assert count(stores, "pool_snapshots") == len(POOLS)
    assert count(stores, "candidate_decisions") == 0
    assert count(stores, "feature_vectors") == 0
    assert count(stores, "audit_events") == audit_before
    universe = asyncio.run(stores.history.latest_universe())
    assert {item.pool.address.root for item in universe} == {pool.address.root for pool in POOLS}


def test_a_current_decision_survives_retention_however_old_it_is(stores: Repositories) -> None:
    harness = Harness()
    candidates = pipeline(harness, stores)

    later = NOW + SYNC * 13

    async def scenario() -> None:
        for index in range(20):
            await sync(stores, NOW - SYNC * 6 + SYNC * index)
        harness.keeper.ensure_open()
        for pool in POOLS:
            await candidates.evaluate(pool.address.root, NOW)
        # Over an hour later the outcome is the same: the old record still stands.
        for pool in POOLS:
            await candidates.evaluate(pool.address.root, later)

    asyncio.run(scenario())
    retention = stores.retention()
    assert retention is not None

    asyncio.run(retention.purge(later + timedelta(minutes=1)))

    assert count(stores, "candidate_decisions") == len(POOLS)


def test_current_outcomes_leave_out_pools_the_last_complete_cycle_skipped(
    stores: Repositories,
) -> None:
    harness = Harness()
    candidates = pipeline(harness, stores)

    async def scenario() -> tuple[Any, Any]:
        for index in range(7):
            await sync(stores, NOW - SYNC * (6 - index))
        harness.keeper.ensure_open()
        for pool in POOLS:
            await candidates.evaluate(pool.address.root, NOW)
        # The next two cycles skip POOL_B (as a pool with an entry in work is skipped).
        for later in (NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)):
            for pool in (POOL, POOL_C):
                await candidates.evaluate(pool.address.root, later)
        outcomes = await stores.journal.latest_outcomes()
        latest = await stores.journal.latest_by_pool([POOL_B.address.root])
        return outcomes, latest

    outcomes, latest = asyncio.run(scenario())

    assert {item.pool_address for item in outcomes} == {POOL.address.root, POOL_C.address.root}
    assert all("LOW_TVL" in item.reasons for item in outcomes)
    # The skipped pool's standing decision is still readable on its own card.
    assert set(latest) == {POOL_B.address.root}


def test_the_decision_journal_has_its_page_and_per_pool_indexes(stores: Repositories) -> None:
    assert stores.database is not None

    async def work(session: Any) -> list[str]:
        rows = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = 'candidate_decisions'")
        )
        return [str(row[0]) for row in rows]

    definitions = stores.database.run(work)

    assert any(item.endswith("(seq)") for item in definitions), definitions
    assert any("(pool_id, seq DESC)" in item for item in definitions), definitions
