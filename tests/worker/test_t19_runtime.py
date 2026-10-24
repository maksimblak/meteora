"""T19 wiring: the worker passes the sale settings, the position rereads of the Data API and
the GMGN developer watch through its runtime, never through the process environment."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.execution.settings import SwapSettings
from src.market.models import DeveloperStatus
from src.observability.metrics import WorkerMetrics
from src.persistence.memory import InMemoryEventSink
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.runtime import WorkerRuntime, WorkerSettings, build_runtime
from tests.domain.guide_fixtures import guide_history
from tests.worker.harness import NOW, POOL, memory_repositories, safe_token
from tests.worker.test_gateway_execution import ingest, run_job
from tests.worker.test_t19_positions import Developer, LiveSource, gmgn


def shadow_runtime(clock: FrozenClock | None = None, **options: Any) -> WorkerRuntime:
    clock = clock or FrozenClock(NOW)
    repos = memory_repositories(clock)
    ingest(repos, *guide_history())
    runtime = WorkerRuntime(
        settings=WorkerSettings(
            metrics_port=None, owner=f"worker-{uuid4()}", shadow_equity_base_units=1_000_000
        ),
        policy=guide_policy(
            GuidePolicySettings(
                baseline_points=6, minimum_baseline_points=6, maximum_history_points=20
            )
        ),
        repositories=repos,
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        sink=InMemoryEventSink(),
        **options,
    )

    async def enter() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(enter())
    assert outcome.entry is not None and outcome.entry.position_id is not None
    return runtime


def test_the_position_job_rereads_the_pool_through_the_runtimes_source() -> None:
    clock = FrozenClock(NOW)
    source = LiveSource(clock, price="0.2")
    runtime = shadow_runtime(clock, pool_snapshots=source)
    clock.advance(timedelta(seconds=30))

    run_job(runtime, "position")

    assert source.requests == [POOL]
    assert runtime.repositories.mode.open_positions() == ()


def test_the_position_job_watches_the_developer_through_the_runtimes_source() -> None:
    developer = Developer(gmgn(status=DeveloperStatus.SOLD))
    runtime = shadow_runtime(developer=developer)

    run_job(runtime, "position")

    assert developer.requests == 1
    assert runtime.repositories.mode.open_positions() == ()


def environ(tmp_path: Path, **variables: str) -> dict[str, str]:
    return {
        "APP_ENV": "development",
        "LOCAL_SQLITE_PATH": str(tmp_path / "worker.sqlite3"),
        **variables,
    }


def built(values: dict[str, str]) -> WorkerRuntime:
    runtime, clients = build_runtime(AppConfig.load(environ=values), values)

    async def close() -> None:
        for client in clients:
            await client.aclose()

    asyncio.run(close())
    runtime.close()
    return runtime


def test_the_sale_settings_come_from_the_runtimes_environment_not_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWAP_AFTER_EXIT", "true")
    monkeypatch.setenv("SWAP_MAX_PRICE_IMPACT_BPS", "900")

    runtime = built(
        environ(
            tmp_path,
            SWAP_AFTER_EXIT="false",
            SWAP_MAX_PRICE_IMPACT_BPS="250",
            WORKER_POSITION_SNAPSHOT_SECONDS="20",
        )
    )

    assert runtime.swap == SwapSettings(enabled=False, max_price_impact_bps=250)
    assert runtime.settings.position_snapshot_interval == timedelta(seconds=20)


def test_the_position_snapshot_cadence_defaults_to_30_seconds(tmp_path: Path) -> None:
    runtime = built(environ(tmp_path))

    assert runtime.settings.position_snapshot_interval == timedelta(seconds=30)
    assert runtime.swap == SwapSettings()


def test_every_gmgn_source_watches_the_developer_and_none_without_one(tmp_path: Path) -> None:
    """T21 (G25): the browser bridge reads the developer's status as the OpenAPI does, so
    DEV_SOLD does not depend on which source serves GMGN."""

    keyed = built(environ(tmp_path, GMGN_API_KEY="key-1"))
    browser = built(environ(tmp_path, GMGN_URL="http://host.docker.internal:8765"))
    plain = built(environ(tmp_path))

    assert keyed.developer is not None and keyed.developer is keyed.gmgn
    assert browser.gmgn is not None and browser.developer is browser.gmgn
    assert plain.gmgn is None and plain.developer is None
