"""Runtime wiring of the live pipeline: universe filter, LB pair reads, SHADOW quotes."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.execution.contracts import QuoteDirection, QuoteRequestV1
from src.persistence.contracts import JobLease
from src.worker.runtime import build_quotes, build_runtime
from src.worker.scheduler import JobContext
from tests.worker.test_live_series import SERIES, at

SWEEP = (SERIES / "sweep_09.json").read_bytes()


class Recorder:
    def __init__(self) -> None:
        self.market: list[httpx.Request] = []
        self.rpc: list[dict[str, Any]] = []

    def serve_market(self, request: httpx.Request) -> httpx.Response:
        self.market.append(request)
        return httpx.Response(200, content=SWEEP, headers={"content-type": "application/json"})

    def serve_rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.rpc.append(body)
        accounts = body["params"][0]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [None] * len(accounts)}}
        )


def synced(tmp_path: Path, **variables: str) -> Recorder:
    environ = {
        "APP_ENV": "development",
        "LOCAL_SQLITE_PATH": str(tmp_path / "worker.sqlite3"),
        "METEORA_DATA_API_BASE_URL": "https://dlmm.datapi.meteora.ag",
        "SOLANA_RPC_URL": "https://rpc.example",
        **variables,
    }
    recorder = Recorder()
    clock = FrozenClock(at("sweep_09"))
    runtime, clients = build_runtime(
        AppConfig.load(environ=environ),
        environ,
        clock=clock,
        market_transport=httpx.MockTransport(recorder.serve_market),
        rpc_transport=httpx.MockTransport(recorder.serve_rpc),
    )
    now = clock.now()
    context = JobContext(
        name="universe_sync",
        correlation_id=uuid4(),
        started_at=now,
        lease=JobLease("universe_sync", "test", 1, now, now + timedelta(minutes=2)),
    )

    async def run() -> None:
        try:
            await runtime.universe_job(context)
        finally:
            for client in clients:
                await client.aclose()
            runtime.close()

    asyncio.run(run())
    return recorder


@pytest.mark.parametrize(
    ("configured", "sent"),
    [({}, "volume_24h>0"), ({"METEORA_UNIVERSE_FILTER": "all"}, None),
     ({"METEORA_UNIVERSE_FILTER": "tvl>=50000"}, "tvl>=50000")],
    ids=["default", "full-sweep", "custom"],
)
def test_the_universe_is_narrowed_by_the_api_unless_a_full_sweep_is_chosen(
    tmp_path: Path, configured: dict[str, str], sent: str | None
) -> None:
    recorder = synced(tmp_path, **configured)

    (request,) = recorder.market
    assert request.url.path == "/pools"
    assert request.url.params.get("filter_by") == sent


def test_the_runtime_hands_the_rpc_pool_state_reader_to_the_data_api_adapter(
    tmp_path: Path,
) -> None:
    recorder = synced(tmp_path)

    # The sweep's pools are anchored on their LB pair accounts in one batched read.
    (call,) = [body for body in recorder.rpc if body["method"] == "getMultipleAccounts"][:1]
    assert len(call["params"][0]) == 17


def test_quotes_are_wired_in_shadow_without_a_wallet() -> None:
    requests: list[httpx.Request] = []

    def executor(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": "quotes_unavailable"})

    shadow = {
        "EXECUTOR_QUOTES_URL": "http://executor-quotes:3000",
        "EXECUTOR_INTERNAL_TOKEN": "internal-1",
    }
    quotes, client = build_quotes(
        AppConfig.load(environ=shadow), shadow, transport=httpx.MockTransport(executor)
    )
    missing, no_client = build_quotes(AppConfig.load(environ={}), {})

    assert quotes is not None and client is not None
    assert (missing, no_client) == (None, None)
    request = QuoteRequestV1(
        pool_address="5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6",
        direction=QuoteDirection.Y_TO_X,
        in_amount="100000000",
    )

    async def ask() -> None:
        try:
            with pytest.raises(Exception, match="QUOTES_UNAVAILABLE"):
                await quotes.quote(request)
        finally:
            await client.aclose()

    asyncio.run(ask())
    (sent,) = requests
    assert (sent.url.host, sent.url.path) == ("executor-quotes", "/v1/quotes")
    assert sent.headers["x-internal-token"] == "internal-1"


def test_the_shadow_stack_starts_a_wallet_less_quote_executor() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    quotes = services["executor-quotes"]

    assert "profiles" not in quotes  # part of the default (shadow) stack
    assert quotes["build"]["dockerfile"] == "Dockerfile.executor"
    assert quotes["environment"]["TRADING_MODE"] == "SHADOW"
    assert not any(name.startswith("WALLET") for name in quotes["environment"])
    assert "ports" not in quotes
    worker = services["worker"]["environment"]
    assert worker["EXECUTOR_QUOTES_URL"] == "${EXECUTOR_QUOTES_URL:-http://executor-quotes:3000}"
    assert worker["METEORA_DATA_API_BASE_URL"].endswith("dlmm.datapi.meteora.ag}")
    for name in ("EXECUTOR_INTERNAL_TOKEN", "EXECUTOR_INTERNAL_TOKEN_FILE"):
        assert worker[name] == quotes["environment"][name] == f"${{{name}:-}}"
