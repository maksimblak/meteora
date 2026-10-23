"""Worker wiring of GMGN (G18): only with the service configured, only for candidates."""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.market.models import (
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    ProviderState,
    TokenRiskSnapshot,
)
from src.observability.metrics import WorkerMetrics
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.runtime import WorkerRuntime, WorkerSettings, build_runtime
from tests.worker.harness import NOW, POOL, harness_policy, memory_repositories, safe_token
from tests.worker.harness import spike_history as harness_spike


@dataclass
class CountingGmgn:
    """An enhanced source that records which mints it was asked about."""

    asked: list[str] = field(default_factory=list)
    health: ProviderHealth = field(
        default_factory=lambda: ProviderHealth(ProviderState.HEALTHY, 0, None, None)
    )

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
        self.asked.append(mint.root)
        return EnhancedTokenSignals(
            insiders_share=None,
            provider="gmgn",
            source_at=NOW - timedelta(minutes=1),
            freshness=Freshness.STALE,
            detail="CHALLENGE_TIMEOUT",
        )


def runtime_with(gmgn: CountingGmgn | None) -> WorkerRuntime:
    clock = FrozenClock(NOW)
    return WorkerRuntime(
        settings=WorkerSettings(metrics_port=None, owner="test"),
        policy=harness_policy(),
        repositories=memory_repositories(clock),
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        gmgn=gmgn,
    )


def evaluate(runtime: WorkerRuntime, history: list[Any]) -> TokenRiskSnapshot:
    async def run() -> TokenRiskSnapshot:
        universe = runtime.repositories.universe
        for item in history:
            stage = await universe.begin(uuid4(), item.observed_at)
            await stage.stage((item,))
            await stage.commit()
        runtime.keeper.ensure_open()
        outcome = await runtime.pipeline.evaluate(POOL.address.root, NOW)
        record = await runtime.repositories.decisions.decision(outcome.decision_id)
        assert record is not None
        return record.token

    return asyncio.run(run())


def test_a_candidate_reads_gmgn_once_and_the_reading_reaches_the_record() -> None:
    gmgn = CountingGmgn()
    runtime = runtime_with(gmgn)

    token = evaluate(runtime, harness_spike())

    assert gmgn.asked == [POOL.base_mint.root]
    assert token.enhanced is not None and token.enhanced.detail == "CHALLENGE_TIMEOUT"
    # The RPC risk itself is unchanged by the merge.
    assert token.holder_concentration == safe_token().holder_concentration


def test_a_pool_that_fails_the_cheap_filters_never_costs_a_gmgn_page() -> None:
    gmgn = CountingGmgn()
    runtime = runtime_with(gmgn)

    token = evaluate(runtime, harness_spike(tvl="10"))  # below the TVL hard filter

    assert gmgn.asked == []
    assert token.enhanced is None


def test_without_the_service_the_token_risk_is_exactly_as_before() -> None:
    token = evaluate(runtime_with(None), harness_spike())

    assert token == safe_token()


def test_the_runtime_wires_the_service_only_when_gmgn_url_is_set(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": "starting"})

    def started(**variables: str) -> tuple[WorkerRuntime, list[httpx.AsyncClient]]:
        environ = {
            "APP_ENV": "development",
            "LOCAL_SQLITE_PATH": str(tmp_path / f"worker-{len(variables)}.sqlite3"),
            **variables,
        }
        return build_runtime(
            AppConfig.load(environ=environ),
            environ,
            clock=FrozenClock(NOW),
            gmgn_transport=httpx.MockTransport(service),
        )

    plain, plain_clients = started()
    wired, wired_clients = started(GMGN_URL="http://gmgn:8080")

    async def run() -> tuple[EnhancedTokenSignals, tuple[str, ...], tuple[str, ...]]:
        try:
            assert plain.gmgn is None
            assert wired.gmgn is not None
            reading = await wired.gmgn.get_token_signals(POOL.base_mint)
            await plain.refresh_health()
            await wired.refresh_health()
            return (
                reading,
                plain.health.readiness(TradingMode.SHADOW).reasons,
                wired.health.readiness(TradingMode.SHADOW).reasons,
            )
        finally:
            for client in (*plain_clients, *wired_clients):
                await client.aclose()
            plain.close()
            wired.close()

    reading, plain_reasons, wired_reasons = asyncio.run(run())

    (request,) = requests
    assert (request.url.host, request.url.port) == ("gmgn", 8080)
    assert request.url.path == f"/v1/tokens/sol/{POOL.base_mint.root}/signals"
    assert reading.detail == "SERVICE_UNAVAILABLE"
    assert "PROVIDER_DEGRADED:gmgn" in wired_reasons
    assert not any("gmgn" in reason for reason in plain_reasons)
    # The loaded rules carry GMGN whether or not the service runs (LIVE fails closed).
    guide = wired.policy.screening.guide
    assert guide is not None and guide.gmgn is not None and guide.gmgn.required_for_live
