"""Worker runtime on durable storage: jobs under leases, and state across restarts.

The same scenario runs on PostgreSQL (separate connection pools model the worker,
the API and a restarted worker) and on the SQLite local-debug backend.
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.market.models import PoolPage
from src.modes import DailyRiskService, KillSwitchPolicy, ModeService, PendingPlan
from src.observability.metrics import WorkerMetrics
from src.persistence.factory import Repositories, open_repositories
from src.persistence.memory import InMemoryEventSink
from src.persistence.testing import integration_database_url, migrated_schema
from src.providers.fixtures import FixtureMarketDataProvider, FixtureOnChainProvider
from src.worker.runtime import WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobOutcome
from tests.worker.harness import NOW, POOL, harness_policy, safe_token, snapshot, spike_history


@pytest.fixture(params=["sqlite", "postgres"])
def opener(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "sqlite":
        path = str(tmp_path / "worker.sqlite3")
        config = AppConfig.load(environ={"APP_ENV": "development"})
        yield lambda clock: open_repositories(
            config, environ={"LOCAL_SQLITE_PATH": path}, clock=clock
        )
        return
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    with migrated_schema(url) as schema:
        config = AppConfig.load(environ={"DATABASE_URL": url})
        yield lambda clock: open_repositories(config, environ={}, clock=clock, schema=schema.schema)


def runtime(repositories: Repositories, clock: FrozenClock, market: Any = None) -> WorkerRuntime:
    return WorkerRuntime(
        settings=WorkerSettings(
            max_concurrency=6,
            metrics_port=None,
            owner=f"worker-{uuid4()}",
            shadow_equity_base_units=1_000_000,
        ),
        policy=harness_policy(),
        repositories=repositories,
        market=market,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        sink=InMemoryEventSink(),
    )


def ingest(repositories: Repositories, clock: FrozenClock) -> None:
    async def stage_all() -> None:
        for item in spike_history():
            stage = await repositories.universe.begin(uuid4(), item.observed_at)
            await stage.stage((item,))
            await stage.commit()

    asyncio.run(stage_all())


def test_all_jobs_run_under_leases_on_durable_storage(opener: Any) -> None:
    clock = FrozenClock(NOW)
    repositories = opener(clock)
    market = FixtureMarketDataProvider(
        pages=[PoolPage(items=(POOL,))],
        snapshots=[snapshot(NOW, short="400", long="1200")],
    )
    try:
        ingest(repositories, clock)
        worker = runtime(repositories, clock, market)

        async def one_tick() -> Any:
            await worker.refresh_health()
            started = await worker.scheduler.tick()
            runs = await worker.scheduler.drain()
            return started, runs

        started, runs = asyncio.run(one_tick())
        assert set(started) == {
            "position_cycle",
            "recovery",
            "candidate_pipeline",
            "universe_sync",
            "outbox_relay",
            "retention",
        }
        assert {run.name: run.outcome for run in runs} == dict.fromkeys(
            started, JobOutcome.SUCCEEDED
        )
        assert len(repositories.mode.open_positions()) == 1
        assert worker.health.readiness(TradingMode.SHADOW).ready is True
    finally:
        repositories.close()


def test_stops_mode_and_open_positions_survive_worker_and_api_restarts(opener: Any) -> None:
    clock = FrozenClock(NOW)
    first = opener(clock)
    api = opener(clock)
    try:
        ingest(first, clock)
        worker = runtime(first, clock)

        async def open_entry() -> None:
            await worker.recovery.reconcile()
            worker.keeper.ensure_open()
            await worker.pipeline.evaluate(POOL.address.root, NOW)

        asyncio.run(open_entry())
        (position_id,) = first.mode.open_positions()
        DailyRiskService(api.mode, clock).record(Decimal("-50000"), Decimal(0), "rules-1")
        ModeService(api.mode, clock, "rules-1").activate_kill_switch(
            "operator stop", KillSwitchPolicy.HOLD_AND_ALERT
        )
    finally:
        first.close()
        api.close()

    clock.advance(timedelta(minutes=1))
    restarted = opener(clock)
    try:
        state = restarted.mode.mode_state()
        assert state.kill_switch_active is True
        assert state.daily_stop_date == NOW.date()
        assert state.mode is TradingMode.SHADOW and state.promotion_authorized is False
        assert restarted.mode.risk_day(NOW.date()).daily_stop_triggered is True
        assert restarted.mode.open_positions() == (position_id,)
        assert restarted.accounting.position_for(position_id).status.value == "OPEN"
        assert asyncio.run(restarted.positions.get(position_id)) is not None
        assert {alert.code for alert in restarted.mode.alerts() if alert.open} == {
            "DAILY_STOP_TRIGGERED",
            "KILL_SWITCH_ACTIVE",
        }
        plan = PendingPlan(
            plan_id=uuid4(),
            correlation_id="after-restart",
            admitted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
        assert set(restarted.mode.admit_entry(plan, 3)) == {
            "KILL_SWITCH_ACTIVE",
            "DAILY_STOP_REACHED",
        }

        again = runtime(restarted, clock)
        report = asyncio.run(again.recovery.reconcile())
        assert report.entries == ()
        cycle = asyncio.run(again.positions.run_all())
        assert restarted.mode.open_positions() == (position_id,)
        assert cycle.risk_day is not None and cycle.risk_day.daily_stop_triggered is True
    finally:
        restarted.close()
