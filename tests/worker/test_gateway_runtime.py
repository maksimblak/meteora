"""Worker startup never wires a money path by default, and an unready executor is reported."""

import asyncio
from pathlib import Path

import httpx
import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.observability.metrics import WorkerMetrics
from src.persistence.factory import PersistenceBackend
from src.persistence.memory import InMemoryEventSink
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.runtime import WorkerRuntime, WorkerSettings, build_gateway, build_runtime
from tests.worker.harness import NOW, POOL, harness_policy, memory_repositories, safe_token

INTERNAL = "integration-test-value"


def test_default_startup_is_shadow_only_and_wires_no_gateway(tmp_path: Path) -> None:
    environ = {"APP_ENV": "development", "LOCAL_SQLITE_PATH": str(tmp_path / "worker.sqlite3")}
    config = AppConfig.load(environ=environ)

    runtime, clients = build_runtime(config, environ, clock=FrozenClock(NOW))
    try:
        asyncio.run(runtime.refresh_health())
        readiness = runtime.health.readiness(TradingMode.APPROVAL)
    finally:
        runtime.close()

    assert runtime.execution is None and clients == []
    assert "EXECUTION_GATEWAY_NOT_CONFIGURED" in readiness.reasons


@pytest.mark.parametrize(
    ("environ", "backend"),
    [
        ({}, PersistenceBackend.POSTGRESQL),
        ({"TRADING_MODE": "APPROVAL"}, PersistenceBackend.POSTGRESQL),
        (
            {
                "TRADING_MODE": "APPROVAL",
                "EXECUTOR_URL": "http://executor.internal",
                "EXECUTOR_INTERNAL_TOKEN": INTERNAL,
            },
            PersistenceBackend.SQLITE,
        ),
    ],
    ids=["shadow", "no-executor-url-or-token", "non-durable-execution-store"],
)
def test_the_gateway_is_wired_only_for_a_configured_durable_money_mode(
    environ: dict[str, str], backend: PersistenceBackend
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made")

    repositories = memory_repositories(FrozenClock(NOW))
    repositories.backend = backend

    wiring, client = build_gateway(
        AppConfig.load(environ=environ),
        environ,
        repositories,
        transport=httpx.MockTransport(refuse),
    )

    assert wiring is None and client is None


def test_an_executor_without_a_wallet_keeps_the_approval_worker_not_ready() -> None:
    seen: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"status": "not_ready"})

    environ = {
        "TRADING_MODE": "APPROVAL",
        "EXECUTOR_URL": "http://executor.internal",
        "EXECUTOR_INTERNAL_TOKEN": INTERNAL,
    }
    clock = FrozenClock(NOW)
    repositories = memory_repositories(clock)
    repositories.backend = PersistenceBackend.POSTGRESQL
    wiring, client = build_gateway(
        AppConfig.load(environ=environ),
        environ,
        repositories,
        transport=httpx.MockTransport(executor),
    )
    assert wiring is not None and client is not None
    runtime = WorkerRuntime(
        settings=WorkerSettings(metrics_port=None, owner="worker-test"),
        policy=harness_policy(),
        repositories=repositories,
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        sink=InMemoryEventSink(),
        gateway=wiring,
    )

    async def check() -> None:
        try:
            await runtime.refresh_health()
        finally:
            await client.aclose()

    asyncio.run(check())

    assert wiring.allowed_mode is TradingMode.APPROVAL
    assert "EXECUTION_GATEWAY_NOT_READY" in runtime.health.readiness(TradingMode.APPROVAL).reasons
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/health/ready")]
