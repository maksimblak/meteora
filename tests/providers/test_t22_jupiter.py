"""T22: Jupiter Tokens API v2 - one search per 100 tokens, readings held, never an error.

The recorded answer is the live one of 2026-10-25 for six of the tokens of the top Meteora
DLMM pools, cut to the fields the bot reads plus a few it does not.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    AuthorityStatus,
    Freshness,
    JupiterTokenReading,
    OrganicScoreLabel,
    ProviderState,
)
from src.providers.jupiter import JUPITER_URL, SEARCH_PATH, JupiterTokensProvider
from tests.gmgn.test_gmgn_openapi import Sleeper, mint_number

NOW = datetime(2026, 10, 25, 10, 30, tzinfo=UTC)
RECORDED = Path(__file__).parent / "fixtures" / "jupiter" / "search_2026-10-25.json"
BODY: list[dict[str, Any]] = json.loads(RECORDED.read_text(encoding="utf-8"))
RAW: dict[str, dict[str, Any]] = {
    item["symbol"]: item
    for item in json.loads(RECORDED.read_text(encoding="utf-8"), parse_float=Decimal)
}
MINT = {symbol: item["id"] for symbol, item in RAW.items()}
UNKNOWN = mint_number(77).root

Handler = Callable[[httpx.Request], httpx.Response]


class Api:
    """The search route; records each request and answers with ``handler``."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler or self.recorded

    @staticmethod
    def recorded(request: httpx.Request) -> httpx.Response:
        asked = set(request.url.params["query"].split(","))
        return httpx.Response(200, json=[item for item in BODY if item["id"] in asked])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == SEARCH_PATH
        self.requests.append(request)
        return self.handler(request)


def scenario(
    api: Api,
    steps: Callable[[JupiterTokensProvider, FrozenClock], Any],
    **options: Any,
) -> Any:
    async def run() -> Any:
        clock = FrozenClock(NOW)
        async with httpx.AsyncClient(
            base_url=JUPITER_URL, transport=httpx.MockTransport(api)
        ) as client:
            provider = JupiterTokensProvider(
                client, clock=clock, sleep=Sleeper(clock), **options
            )
            return await steps(provider, clock)

    return asyncio.run(run())


def read(provider: JupiterTokensProvider, symbol_or_mint: str) -> JupiterTokenReading:
    reading = provider.reading(PublicKey(MINT.get(symbol_or_mint, symbol_or_mint)))
    assert reading is not None, symbol_or_mint
    return reading


def test_one_search_reads_every_listed_token_and_names_the_unlisted_one() -> None:
    api = Api()

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> dict[str, Any]:
        await provider.prefetch([*MINT.values(), UNKNOWN])
        return {name: read(provider, name) for name in [*MINT, UNKNOWN]}

    readings = scenario(api, steps)

    (request,) = api.requests
    assert request.url.params["query"].split(",") == [*MINT.values(), UNKNOWN]
    assert set(readings) - {UNKNOWN} == {"BTC", "ORE", "TTWO", "MUSK", "Chubby", "tSpaceX"}
    for symbol in MINT:
        assert readings[symbol].freshness is Freshness.FRESH, symbol
        assert readings[symbol].source_at == NOW
    unknown = readings[UNKNOWN]
    assert (unknown.freshness, unknown.detail) == (Freshness.STALE, "NOT_LISTED")


def test_the_windows_are_buy_plus_sell_and_the_counts_buys_plus_sells() -> None:
    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> JupiterTokenReading:
        await provider.prefetch([MINT["BTC"]])
        return read(provider, "BTC")

    reading = scenario(Api(), steps)
    short, hour, audit = RAW["BTC"]["stats5m"], RAW["BTC"]["stats1h"], RAW["BTC"]["audit"]

    # The decimals are the JSON text of the numbers, never a binary float.
    assert reading.volume_5m_usd == short["buyVolume"] + short["sellVolume"]
    assert reading.volume_1h_usd == hour["buyVolume"] + hour["sellVolume"]
    assert reading.trades_5m == 1822 + 1829
    assert reading.traders_5m == 881
    assert reading.market_cap_usd == RAW["BTC"]["mcap"]
    assert reading.organic_score == RAW["BTC"]["organicScore"]
    assert reading.organic_score_label is OrganicScoreLabel(RAW["BTC"]["organicScoreLabel"])
    assert reading.top_holders_share == audit["topHoldersPercentage"] / 100
    assert reading.first_pool_at == datetime(2026, 9, 13, 19, 45, 18, tzinfo=UTC)
    assert reading.holder_count == 12100


def test_an_authority_is_revoked_only_when_disabled_and_present_only_when_named() -> None:
    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> Any:
        await provider.prefetch([MINT["BTC"], MINT["ORE"], MINT["TTWO"]])
        return [read(provider, name) for name in ("BTC", "ORE", "TTWO")]

    btc, ore, ttwo = scenario(Api(), steps)

    # BTC: both disabled. ORE: freeze disabled; no mintAuthorityDisabled, the authority named.
    assert (btc.mint_authority, btc.freeze_authority) == (
        AuthorityStatus.REVOKED,
        AuthorityStatus.REVOKED,
    )
    assert "mintAuthorityDisabled" not in RAW["ORE"]["audit"] and RAW["ORE"]["mintAuthority"]
    assert (ore.mint_authority, ore.freeze_authority) == (
        AuthorityStatus.PRESENT,
        AuthorityStatus.REVOKED,
    )
    # TTWO (a tokenized stock): both authorities live and named.
    assert (ttwo.mint_authority, ttwo.freeze_authority) == (
        AuthorityStatus.PRESENT,
        AuthorityStatus.PRESENT,
    )


def test_a_window_without_trades_is_zero_and_an_absent_percentage_is_unknown() -> None:
    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> Any:
        await provider.prefetch([MINT["tSpaceX"], MINT["TTWO"], MINT["MUSK"]])
        return [read(provider, name) for name in ("tSpaceX", "TTWO", "MUSK")]

    quiet, stock, musk = scenario(Api(), steps)

    # Jupiter writes a window without a trade as {"volumeChange": -100}: zeros left out.
    assert RAW["tSpaceX"]["stats5m"] == {"volumeChange": -100}
    assert (quiet.volume_5m_usd, quiet.trades_5m, quiet.traders_5m) == (Decimal(0), 0, 0)
    assert quiet.organic_score_label is OrganicScoreLabel.LOW
    # No devBalancePercentage: unknown, not "the developer holds nothing".
    assert "devBalancePercentage" not in RAW["TTWO"]["audit"]
    assert stock.developer_share is None
    assert musk.developer_share == RAW["MUSK"]["audit"]["devBalancePercentage"] / 100


def test_more_tokens_than_one_search_takes_go_in_searches_of_a_hundred() -> None:
    api = Api(lambda request: httpx.Response(200, json=[]))
    mints = [mint_number(1000 + index).root for index in range(150)]

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> None:
        await provider.prefetch(mints)

    scenario(api, steps)

    assert [len(item.url.params["query"].split(",")) for item in api.requests] == [100, 50]


def test_the_keyless_rate_stays_below_half_a_request_a_second() -> None:
    api = Api(lambda request: httpx.Response(200, json=[]))
    mints = [mint_number(2000 + index).root for index in range(300)]
    waited: list[timedelta] = []

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> None:
        start = clock.now()
        await provider.prefetch(mints)
        waited.append(clock.now() - start)

    scenario(api, steps)

    # Three searches at 80% of 0.5 a second: 2.5 s apart, the first at once.
    assert len(api.requests) == 3
    assert waited == [timedelta(seconds=5)]


def test_a_good_reading_is_held_and_asked_again_only_after_it_expires() -> None:
    api = Api()

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> Any:
        await provider.prefetch([MINT["BTC"]])
        clock.advance(timedelta(seconds=119))
        await provider.prefetch([MINT["BTC"]])
        held = len(api.requests)
        clock.advance(timedelta(seconds=1))
        expired = provider.reading(PublicKey(MINT["BTC"]))
        await provider.prefetch([MINT["BTC"]])
        return held, expired, read(provider, "BTC").source_at

    held, expired, again = scenario(api, steps)

    assert held == 1
    assert expired is None  # two minutes old: no longer fresh, not served as fresh
    assert len(api.requests) == 2
    assert again == NOW + timedelta(seconds=120)


def test_a_429_pauses_every_search_until_the_reset() -> None:
    reset = int((NOW + timedelta(seconds=30)).timestamp())
    api = Api(
        lambda request: httpx.Response(429, headers={"x-ratelimit-reset": str(reset)})
    )

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> Any:
        await provider.prefetch([MINT["BTC"]])
        first = read(provider, "BTC")
        health = provider.health
        clock.advance(timedelta(seconds=20))
        await provider.prefetch([MINT["ORE"]])
        paused = (len(api.requests), read(provider, "ORE").detail)
        clock.advance(timedelta(seconds=11))
        await provider.prefetch([MINT["ORE"]])
        return first, health, paused, len(api.requests)

    first, health, paused, total = scenario(api, steps)

    assert (first.freshness, first.detail) == (Freshness.STALE, "RATE_LIMITED")
    assert health.state is ProviderState.OPEN
    # 20 s later the pause (30 s and a second) still holds: nothing is sent.
    assert paused == (1, "RATE_LIMITED")
    assert total == 2


def test_an_outage_a_refusal_or_a_new_format_is_a_reading_never_an_error() -> None:
    answers: list[httpx.Response | Exception] = [
        httpx.Response(500),
        httpx.Response(403),
        httpx.Response(200, json={"error": "changed"}),
        httpx.Response(200, text="not json"),
        httpx.ConnectTimeout("slow"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> list[str | None]:
        details = []
        for _ in range(5):
            await provider.prefetch([MINT["BTC"]])
            details.append(read(provider, "BTC").detail)
            clock.advance(timedelta(minutes=2))
        return details

    details = scenario(Api(handler), steps, failure_threshold=10)

    assert details == [
        "API_UNAVAILABLE",
        "HTTP_403",
        "API_PAYLOAD_INVALID",
        "API_PAYLOAD_INVALID",
        "API_TIMEOUT",
    ]


def test_a_free_key_goes_only_in_its_header_and_doubles_the_rate() -> None:
    api = Api(lambda request: httpx.Response(200, json=[]))
    key = "jup-free-key-0123456789"
    mints = [mint_number(3000 + index).root for index in range(200)]
    waited: list[timedelta] = []

    async def steps(provider: JupiterTokensProvider, clock: FrozenClock) -> None:
        start = clock.now()
        await provider.prefetch(mints)
        waited.append(clock.now() - start)

    scenario(api, steps, api_key=SecretStr(key))

    assert all(item.headers["x-api-key"] == key for item in api.requests)
    assert all(key not in str(item.url) for item in api.requests)
    # Two searches at 80% of 1 a second: 1.25 s apart.
    assert waited == [timedelta(seconds=1.25)]
