"""Which GMGN source the worker uses (G20): key -> OpenAPI, else ``GMGN_URL`` -> browser, else none.

The key is a secret: it reaches neither the api container, nor the effective configuration,
nor the decision record. Only candidates (after the cheap filters) cost OpenAPI requests.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import yaml
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.gmgn import GmgnOpenApiProvider, GmgnProvider
from src.market.models import EnhancedTokenSignals, Freshness
from src.observability.metrics import WorkerMetrics
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.runtime import WorkerRuntime, WorkerSettings, build_runtime
from tests.api.support import make_panel
from tests.gmgn.test_gmgn_openapi import default_info, default_security
from tests.gmgn.test_gmgn_runtime import evaluate
from tests.worker.harness import NOW, POOL, harness_policy, memory_repositories, safe_token
from tests.worker.harness import spike_history as harness_spike

KEY = "test-gmgn-key-fedcba9876543210"  # a stand-in value, not a real key
GMGN_API_NAMES = (
    "GMGN_API_KEY",
    "GMGN_API_KEY_FILE",
    "GMGN_API_URL",
    "GMGN_API_PLAN",
    "GMGN_API_CACHE_SECONDS",
)


class Recorder:
    """Both GMGN sources behind one mock transport: the OpenAPI host and the gmgn service."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/token/info":
            return default_info(request)
        if request.url.path == "/v1/token/security":
            return default_security(request)
        return httpx.Response(503, json={"error": "starting"})


def started(
    tmp_path: Path, recorder: Recorder, **variables: str
) -> tuple[WorkerRuntime, EnhancedTokenSignals | None]:
    environ = {
        "APP_ENV": "development",
        "LOCAL_SQLITE_PATH": str(tmp_path / f"worker-{len(variables)}-{len(recorder.requests)}.db"),
        **variables,
    }
    runtime, clients = build_runtime(
        AppConfig.load(environ=environ),
        environ,
        clock=FrozenClock(NOW),
        gmgn_transport=httpx.MockTransport(recorder),
    )

    async def ask() -> EnhancedTokenSignals | None:
        try:
            if runtime.gmgn is None:
                return None
            return await runtime.gmgn.get_token_signals(POOL.base_mint)
        finally:
            for client in clients:
                await client.aclose()

    try:
        return runtime, asyncio.run(ask())
    finally:
        runtime.close()


def test_with_a_key_the_worker_reads_the_official_api_even_when_the_browser_is_set(
    tmp_path: Path,
) -> None:
    recorder = Recorder()

    runtime, reading = started(tmp_path, recorder, GMGN_API_KEY=KEY, GMGN_URL="http://gmgn:8080")

    assert isinstance(runtime.gmgn, GmgnOpenApiProvider)
    assert [request.url.host for request in recorder.requests] == ["openapi.gmgn.ai"] * 2
    assert recorder.requests[0].headers["X-APIKEY"] == KEY
    assert reading is not None and reading.freshness is Freshness.FRESH
    assert reading.phishing_share == Decimal("0.7367")
    # The loaded rules keep the LIVE requirement whatever the source.
    guide = runtime.policy.screening.guide
    assert guide is not None and guide.gmgn is not None and guide.gmgn.required_for_live


def test_the_key_file_and_the_configured_address_are_honoured(tmp_path: Path) -> None:
    key_file = tmp_path / "gmgn_api_key"
    key_file.write_text(KEY, encoding="utf-8")
    recorder = Recorder()

    runtime, reading = started(
        tmp_path,
        recorder,
        GMGN_API_KEY_FILE=str(key_file),
        GMGN_API_URL="https://gmgn-api.example.org",
    )

    assert isinstance(runtime.gmgn, GmgnOpenApiProvider)
    assert {request.url.host for request in recorder.requests} == {"gmgn-api.example.org"}
    assert recorder.requests[0].headers["X-APIKEY"] == KEY
    assert reading is not None and reading.detail is None


def test_without_a_key_the_browser_service_is_the_fallback(tmp_path: Path) -> None:
    recorder = Recorder()

    runtime, reading = started(tmp_path, recorder, GMGN_URL="http://gmgn:8080")

    assert isinstance(runtime.gmgn, GmgnProvider)
    (request,) = recorder.requests
    assert (request.url.host, request.url.port) == ("gmgn", 8080)
    assert request.url.path == f"/v1/tokens/sol/{POOL.base_mint.root}/signals"
    assert "X-APIKEY" not in request.headers
    assert reading is not None and reading.detail == "SERVICE_UNAVAILABLE"


def test_with_neither_nothing_is_asked_as_before(tmp_path: Path) -> None:
    recorder = Recorder()

    runtime, reading = started(tmp_path, recorder)

    assert runtime.gmgn is None and reading is None
    assert recorder.requests == []


def test_only_candidates_cost_openapi_requests_and_the_record_holds_no_key() -> None:
    recorder = Recorder()
    clock = FrozenClock(NOW)
    client = httpx.AsyncClient(
        base_url="https://openapi.gmgn.ai", transport=httpx.MockTransport(recorder)
    )

    def runtime() -> WorkerRuntime:
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
            gmgn=GmgnOpenApiProvider(client, api_key=SecretStr(KEY), clock=clock),
        )

    try:
        filtered = evaluate(runtime(), harness_spike(tvl="10"))  # fails the TVL hard filter
        assert recorder.requests == []
        candidate = evaluate(runtime(), harness_spike())
    finally:
        asyncio.run(client.aclose())

    assert filtered.enhanced is None
    assert [request.url.path for request in recorder.requests] == [
        "/v1/token/info",
        "/v1/token/security",
    ]
    assert candidate.enhanced is not None and candidate.enhanced.detail is None
    assert candidate.enhanced.insiders_share == Decimal("0.0001")
    # D03: the official total fees reach the token risk the thresholds read.
    assert candidate.total_fees_sol == Decimal("1223.6575276103256")
    assert KEY not in candidate.model_dump_json()


def test_the_key_goes_to_the_worker_only_and_never_to_the_effective_config(
    tmp_path: Path,
) -> None:
    services = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))["services"]
    worker = services["worker"]["environment"]
    api = services["api"]["environment"]
    panel = make_panel(tmp_path, environ={"GMGN_API_KEY": KEY})
    try:
        response = panel.get("/api/v1/config/effective")
    finally:
        panel.repositories.close()

    for name in GMGN_API_NAMES:
        assert worker[name] == f"${{{name}:-}}"
        assert name not in api
    assert response.status_code == 200
    assert KEY not in response.text


def test_example_environment_names_the_openapi_settings_and_where_the_key_is_made() -> None:
    lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
    names = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines if "=" in line}

    for name in GMGN_API_NAMES:
        assert names[name] == ""
    assert any(line.startswith("#") and "gmgn.ai/ai" in line for line in lines)
