"""T21 (G25): ``GmgnProvider`` reads the browser bridge's ``gmgn-signals.v2`` answer.

The bridge answers the same metrics the OpenAPI provider reads (T18), so the provider fills
the same ``EnhancedTokenSignals`` fields, keeps good readings for the F5 rank
(``cached_signals``) and sends the shared bridge token - which never reaches a reading.
Expected values are those of ``contract/signals_v2_available.json`` (the bridge's answer for
a live recording of 2026-10-24), read off the file by hand.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.gmgn import GmgnProvider
from src.market.models import DeveloperStatus, EnhancedTokenSignals, Freshness, RiskSignal

CONTRACT = Path(__file__).parent / "contract"
AVAILABLE: dict[str, Any] = json.loads(
    (CONTRACT / "signals_v2_available.json").read_text(encoding="utf-8")
)
GROK = PublicKey(AVAILABLE["mint"])
OTHER = PublicKey("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
RECORDED = datetime(2026, 10, 24, 16, 50, tzinfo=UTC)
TOKEN = "t21-worker-bridge-token-0123456789"  # noqa: S105 - a test value


def v2(**signals: Any) -> dict[str, Any]:
    body = json.loads(json.dumps(AVAILABLE))
    body["signals"].update(signals)
    return body


class Bridge:
    def __init__(self, *bodies: Any, status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return httpx.Response(self.status, json=body)


def provider_run(
    bridge: Bridge,
    work: Any,
    *,
    clock: FrozenClock | None = None,
    **options: Any,
) -> Any:
    clock = clock or FrozenClock(RECORDED + timedelta(minutes=1))

    async def run() -> Any:
        async with httpx.AsyncClient(
            base_url="http://host.docker.internal:8765", transport=httpx.MockTransport(bridge)
        ) as client:
            provider = GmgnProvider(client, clock=clock, **options)
            return await work(provider, clock)

    return asyncio.run(run())


async def one(provider: GmgnProvider, clock: FrozenClock) -> EnhancedTokenSignals:
    return await provider.get_token_signals(GROK)


def test_a_v2_answer_fills_the_fields_the_openapi_provider_fills() -> None:
    signals = provider_run(Bridge(AVAILABLE), one)

    assert signals.provider == "gmgn"
    assert signals.freshness is Freshness.FRESH and signals.detail is None
    assert signals.source_at == RECORDED
    assert signals.insiders_share == Decimal("0.7968")
    assert signals.developer_share == Decimal("0.0325267424")
    assert signals.total_fees_sol == Decimal("0.9718177443108398")
    assert signals.volume_5m_usd == Decimal("7419.35651281")
    assert signals.volume_1h_usd == Decimal("137356.48892064")
    assert signals.price_usd == Decimal("0.00016726742")
    assert signals.market_cap_usd == Decimal("166495.96670650423973744")
    assert signals.ath_price_usd == Decimal("0.0008064953499703052620352908572")
    assert signals.ath_market_cap_usd == Decimal("802775.7165")
    assert signals.sniper_count == 20
    assert signals.sniper_share == Decimal("0.0325267932")
    assert signals.developer_status is DeveloperStatus.SOLD
    assert signals.created_at == datetime(2026, 10, 24, 15, 56, 15, tzinfo=UTC)
    assert signals.wash_trading is RiskSignal.SAFE
    assert signals.missing_signals == ()


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, RiskSignal.UNSAFE), (False, RiskSignal.SAFE), (None, RiskSignal.UNKNOWN)],
)
def test_the_wash_trading_verdict_is_kept_and_an_unknown_one_is_never_clean(
    flag: bool | None, expected: RiskSignal
) -> None:
    signals = provider_run(Bridge(v2(wash_trading=flag)), one)

    assert signals.wash_trading is expected


def test_a_market_figure_in_a_new_format_is_dropped_and_the_reading_kept() -> None:
    body = v2(
        volume_1h_usd=137356.48,  # a binary float
        sniper_count="20",
        sniper_share="1.5",
        developer_status="MAYBE",
        created_at="yesterday",
        ath_market_cap_usd="-1",
        wash_trading="no",
    )

    signals = provider_run(Bridge(body), one)

    assert signals.freshness is Freshness.FRESH and signals.detail is None
    assert (signals.volume_1h_usd, signals.sniper_count, signals.sniper_share) == (None, None, None)
    assert (signals.developer_status, signals.created_at, signals.ath_market_cap_usd) == (
        None,
        None,
        None,
    )
    assert signals.wash_trading is RiskSignal.UNKNOWN
    assert signals.volume_5m_usd == Decimal("7419.35651281")
    assert signals.missing_signals == ()  # the six v1 shares and fees are all there


def test_a_good_reading_ranks_tokens_from_the_cache_without_a_request() -> None:
    bridge = Bridge(AVAILABLE)

    async def work(provider: GmgnProvider, clock: FrozenClock) -> list[Any]:
        before = provider.cached_signals(GROK)
        first = await provider.get_token_signals(GROK)
        again = await provider.get_token_signals(GROK)
        held = provider.cached_signals(GROK)
        other = provider.cached_signals(OTHER)
        clock.advance(timedelta(minutes=4))  # source_at + 5 min = the cache's end
        expired = provider.cached_signals(GROK)
        return [before, first, again, held, other, expired]

    before, first, again, held, other, expired = provider_run(
        bridge, work, cache_ttl=timedelta(minutes=5)
    )

    assert before is None
    assert again == first and held == first
    assert len(bridge.requests) == 1
    assert other is None and expired is None


def test_a_stale_or_refused_reading_is_never_served_from_the_cache() -> None:
    stale = {**AVAILABLE, "freshness": "STALE", "reason": "CHALLENGE"}
    refused = json.loads((CONTRACT / "signals_v2_unavailable.json").read_text(encoding="utf-8"))

    async def work(provider: GmgnProvider, clock: FrozenClock) -> list[Any]:
        first = await provider.get_token_signals(GROK)
        second = await provider.get_token_signals(GROK)
        return [first, second, provider.cached_signals(GROK)]

    first, second, cached = provider_run(Bridge(stale, refused), work)

    assert first.freshness is Freshness.STALE
    assert second.detail == "CHALLENGE"
    assert cached is None


def test_the_shared_token_rides_in_the_header_and_nowhere_else() -> None:
    bridge = Bridge(AVAILABLE)

    async def work(provider: GmgnProvider, clock: FrozenClock) -> list[Any]:
        return [await provider.get_token_signals(GROK), repr(provider), provider.health]

    signals, text, health = provider_run(bridge, work, token=SecretStr(TOKEN))

    (request,) = bridge.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert TOKEN not in text and TOKEN not in signals.model_dump_json()
    assert TOKEN not in repr(health)


def test_a_refused_bridge_token_is_its_own_reason() -> None:
    bridge = Bridge({"error": {"code": "UNAUTHORIZED"}}, status=401)

    signals = provider_run(bridge, one, token=SecretStr("not-the-bridge-token-0000"))

    assert signals.freshness is Freshness.STALE
    assert signals.detail == "SERVICE_UNAUTHORIZED"


def test_the_shared_v2_corpus_parses_on_the_worker_side() -> None:
    refused = json.loads((CONTRACT / "signals_v2_unavailable.json").read_text(encoding="utf-8"))

    signals = provider_run(Bridge(refused), one)

    assert signals.freshness is Freshness.STALE
    assert signals.detail == "CHALLENGE"
    assert signals.volume_1h_usd is None and signals.developer_status is None
