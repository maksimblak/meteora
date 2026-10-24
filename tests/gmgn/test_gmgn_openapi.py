"""``GmgnOpenApiProvider``: GMGN metrics from the official GMGN OpenAPI by the user's key (G20).

``openapi/token_info_wif.json`` and ``openapi/token_security_wif.json`` are live answers of
``GET /v1/token/info`` and ``GET /v1/token/security`` for WIF (2026-10-24), trimmed to the
fields around the ones read. The 401 envelope below is a live answer too; the 429 and 403
bodies follow the official documentation (GMGNAI/gmgn-skills: ``skills/gmgn-token/SKILL.md``,
``docs/cli-usage.md``). No test here goes to the network.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.api.presenters import gmgn_metrics
from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, DecisionFlag, RejectionCode
from src.gmgn import openapi
from src.gmgn.openapi import (
    API_KEY_HEADER,
    OPENAPI_URL,
    GmgnOpenApiProvider,
    GmgnPlan,
    OpenApiSettings,
    ipv4_transport,
    openapi_client,
)
from src.market.models import EnhancedTokenSignals, Freshness, ProviderState, RiskSignal
from tests.domain.guide_fixtures import NOW as GUIDE_NOW
from tests.gmgn.test_gmgn_screening import evaluate, evidence, token_with

NOW = datetime(2026, 10, 24, 7, 15, 31, tzinfo=UTC)
WIF = PublicKey("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm")
BONK = PublicKey("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
KEY = "test-gmgn-key-0123456789abcdef"  # a stand-in value, not a real key
FIXTURES = Path(__file__).parent / "openapi"
INFO = "/v1/token/info"
SECURITY = "/v1/token/security"
SIX = (
    "top10_holder_share",
    "insiders_share",
    "bundling_share",
    "phishing_share",
    "developer_share",
    "total_fees_sol",
)

Answer = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def mint_number(number: int) -> PublicKey:
    """A distinct valid public key per number (base58 of 32 bytes)."""

    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    value = int.from_bytes(bytes([7] * 28) + number.to_bytes(4, "big"), "big")
    text = ""
    while value:
        value, rest = divmod(value, 58)
        text = alphabet[rest] + text
    return PublicKey(text)


def fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return body


def info_body(mint: PublicKey = WIF, *, drop: Sequence[str] = (), **stat: object) -> dict[str, Any]:
    body = fixture("token_info_wif.json")
    body["data"]["address"] = mint.root
    for name in drop:
        body["data"]["stat"].pop(name, None)
    body["data"]["stat"].update(stat)
    return body


def security_body(mint: PublicKey = WIF, **changes: object) -> dict[str, Any]:
    body = fixture("token_security_wif.json")
    body["data"]["address"] = mint.root
    body["data"].update(changes)
    return body


def asked(request: httpx.Request) -> PublicKey:
    return PublicKey(request.url.params["address"])


def default_info(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=info_body(asked(request)))


def default_security(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=security_body(asked(request)))


class Api:
    """The OpenAPI as the provider sees it: queued answers per route (the last one repeats)."""

    def __init__(
        self,
        clock: FrozenClock,
        *,
        info: Sequence[Answer] = (default_info,),
        security: Sequence[Answer] = (default_security,),
    ) -> None:
        self.clock = clock
        self.answers = {INFO: list(info), SECURITY: list(security)}
        self.requests: list[httpx.Request] = []
        self.sent_at: list[datetime] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.sent_at.append(self.clock.now())
        queue = self.answers[request.url.path]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return answer(request)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


class Sleeper:
    """``asyncio.sleep`` on the frozen clock: waiting moves time, nothing really sleeps."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


Step = PublicKey | timedelta


def run(
    api: Api,
    steps: Sequence[Step],
    *,
    clock: FrozenClock,
    sleeper: Sleeper | None = None,
    **options: Any,
) -> tuple[list[EnhancedTokenSignals], GmgnOpenApiProvider]:
    """Readings for the mints in ``steps``; a ``timedelta`` step moves the clock."""

    async def scenario() -> tuple[list[EnhancedTokenSignals], GmgnOpenApiProvider]:
        async with httpx.AsyncClient(
            base_url=OPENAPI_URL, transport=httpx.MockTransport(api)
        ) as client:
            provider = GmgnOpenApiProvider(
                client,
                api_key=SecretStr(KEY),
                clock=clock,
                sleep=sleeper or Sleeper(clock),
                **options,
            )
            readings = []
            for step in steps:
                if isinstance(step, timedelta):
                    clock.advance(step)
                else:
                    readings.append(await provider.get_token_signals(step))
            return readings, provider

    return asyncio.run(scenario())


def error(status: int, body: object, headers: dict[str, str] | None = None) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(status, text=body, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


# --- the reading ------------------------------------------------------------------------


def test_the_official_answer_becomes_the_same_enhanced_signals_as_the_browser_path() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock)

    (signals,), provider = run(api, [WIF], clock=clock)

    assert api.paths == [INFO, SECURITY]
    for request in api.requests:
        assert request.method == "GET"
        assert request.url.host == "openapi.gmgn.ai"
        assert request.url.params["chain"] == "sol"
        assert request.url.params["address"] == WIF.root
        assert request.url.params["timestamp"] == str(int(NOW.timestamp()))
        uuid.UUID(request.url.params["client_id"])
        assert request.headers[API_KEY_HEADER] == KEY
        assert KEY not in str(request.url)
    assert api.requests[0].url.params["client_id"] != api.requests[1].url.params["client_id"]
    # stat.* of token info, as named by spec 6.7 (G20) and the browser service.
    assert signals.insiders_share == Decimal("0.0001")
    assert signals.bundling_share == Decimal("0.0027")
    assert signals.phishing_share == Decimal("0.7367")
    assert signals.top10_holder_share == Decimal("0.323")
    assert signals.developer_share == Decimal("0")
    # ``total_fee`` of token info: SOL, the token page's "Total Fees" (D03).
    assert signals.total_fees_sol == Decimal("1223.6575276103256")
    assert signals.missing_signals == ()
    assert signals.detail is None
    assert signals.freshness is Freshness.FRESH
    assert signals.source_at == NOW
    assert signals.provider.startswith("gmgn")
    # Like the browser path: no blacklist, verification or wash-trading verdict.
    assert signals.holder_concentration is None
    assert signals.wash_trading is RiskSignal.UNKNOWN
    assert provider.health.state is ProviderState.HEALTHY


def busy_clean_info(request: httpx.Request) -> httpx.Response:
    """WIF with clean holders and a token volume of 20 000 a minute now and over the hour."""

    body = info_body(
        asked(request), top_entrapment_trader_percentage="0.05", top_10_holder_rate="0.15"
    )
    body["data"]["price"].update(volume_5m="100000", volume_1h="1200000")
    return httpx.Response(200, json=body)


def test_the_live_wif_answer_meets_the_unchanged_guide_thresholds_and_panel() -> None:
    clock = FrozenClock(GUIDE_NOW - timedelta(minutes=1))
    clean = Api(clock, info=(busy_clean_info,))

    (wif,), _ = run(Api(clock), [WIF], clock=clock)
    (fine,), _ = run(clean, [WIF], clock=clock)
    rejected = evaluate(token_with(wif), mode=TradingMode.LIVE)
    accepted = evaluate(token_with(fine), mode=TradingMode.LIVE)

    # WIF on 2026-10-24: phishing 73.67% and top-10 32.3% fail the guide-v1 maxima, and
    # (G24, by token) its GMGN volume - 1 542 in 5m, 1 733 in the hour - is far from
    # 14 000 a minute, whatever the one DLMM pool of the fixture shows.
    assert rejected.status is CandidateStatus.REJECT
    assert set(rejected.reasons) == {
        RejectionCode.PHISHING_SHARE_HIGH,
        RejectionCode.GMGN_TOP10_HOLDER_SHARE_HIGH,
        RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD,
        RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD,
    }
    assert DecisionFlag.GMGN_UNAVAILABLE not in rejected.flags
    found = evidence(rejected)
    assert found["gmgn_status"] == ("FRESH", None)
    assert found["insiders_share"] == ("0.0001", "0.10")
    assert found["bundling_share"] == ("0.0027", "0.70")
    assert found["phishing_share"] == ("0.7367", "0.30")
    assert found["gmgn_top10_holder_share"] == ("0.323", "0.30")
    assert found["developer_share"] == ("0", "0")
    assert found["total_fees_sol"] == ("1223.6575276103256", "30")
    # With the key LIVE no longer stops at GMGN_UNAVAILABLE. WIF (created 2023-11-20, 1 000
    # sniper wallets) is flagged as a revival with many snipers; the fixture's 5m spikes.
    assert accepted.status is CandidateStatus.ACCEPT
    assert set(accepted.flags) == {
        DecisionFlag.VOLUME_SPIKE,
        DecisionFlag.SNIPERS_HIGH,
        DecisionFlag.TOKEN_REVIVED,
    }
    shown = {item.name for item in gmgn_metrics(accepted.evidence)}
    assert {"gmgn_status", "insiders_share", "bundling_share", "phishing_share"} <= shown
    assert {"gmgn_top10_holder_share", "developer_share", "total_fees_sol"} <= shown


def test_security_only_fills_what_the_info_answer_lacks() -> None:
    clock = FrozenClock(NOW)
    thin_info = info_body(
        drop=("top_10_holder_rate", "top_rat_trader_percentage", "top_bundler_trader_percentage"),
        creator_hold_rate="0.01",
    )
    rich_security = security_body(
        top_10_holder_rate="0.25",
        rat_trader_amount_rate="0.03",
        bundler_trader_amount_rate="0.4",
        dev_team_hold_rate="0.02",
        creator_balance_rate="0.015",
    )
    filled = Api(
        clock,
        info=(httpx.Response(200, json=thin_info),),
        security=(httpx.Response(200, json=rich_security),),
    )
    ignored = Api(clock, security=(httpx.Response(200, json=rich_security),))
    no_security_top10 = info_body(drop=("top_10_holder_rate",))
    fallback = Api(
        clock,
        info=(httpx.Response(200, json=no_security_top10),),
        security=(httpx.Response(200, json=security_body(top_10_holder_rate=None)),),
    )

    (thin,), _ = run(filled, [WIF], clock=clock)
    (full,), _ = run(ignored, [WIF], clock=clock)
    (dev_top10,), _ = run(fallback, [WIF], clock=clock)

    assert thin.top10_holder_share == Decimal("0.25")
    assert thin.insiders_share == Decimal("0.03")
    assert thin.bundling_share == Decimal("0.4")
    # The developer share is never under-reported: the largest holding reported.
    assert thin.developer_share == Decimal("0.02")
    assert (full.top10_holder_share, full.insiders_share) == (Decimal("0.323"), Decimal("0.0001"))
    assert full.developer_share == Decimal("0.02")
    # Last resort for top-10: ``dev.top_10_holder_rate`` of token info.
    assert dev_top10.top10_holder_share == Decimal("0.323")


def test_a_failed_security_answer_keeps_the_info_reading() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, security=(error(502, {"code": 502, "error": "INTERNAL_API_UNAVAILABLE"}),))

    (signals,), _ = run(api, [WIF], clock=clock)

    assert api.paths == [INFO, SECURITY]
    assert signals.freshness is Freshness.FRESH and signals.detail is None
    assert signals.phishing_share == Decimal("0.7367")


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(200, text="<html>Just a moment...</html>"), "API_PAYLOAD_INVALID"),
        (
            httpx.Response(200, json={"code": 0, "data": ["not", "a", "token"]}),
            "API_PAYLOAD_INVALID",
        ),
        (
            httpx.Response(200, json={"code": 0, "data": {"address": BONK.root}}),
            "API_MINT_MISMATCH",
        ),
        (httpx.Response(200, json={"code": 40001, "error": "BAD_REQUEST"}), "BAD_REQUEST"),
        (error(400, {"code": 400, "error": "CHAIN_NOT_SUPPORTED"}), "CHAIN_NOT_SUPPORTED"),
        (
            error(502, {"code": 502, "error": "INTERNAL_API_UNAVAILABLE"}),
            "INTERNAL_API_UNAVAILABLE",
        ),
        (error(500, "upstream exploded"), "API_UNAVAILABLE"),
        (httpx.ReadTimeout("slow"), "API_TIMEOUT"),
        (httpx.ConnectError("refused"), "API_UNAVAILABLE"),
    ],
    ids=["html", "array", "mint", "code", "http-400", "http-502", "http-500", "timeout", "refused"],
)
def test_every_failure_of_the_info_answer_degrades_instead_of_raising(
    answer: Answer, detail: str
) -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(answer,))

    (signals,), provider = run(api, [WIF], clock=clock)

    assert api.paths == [INFO]  # no security request without the info reading
    assert signals.freshness is Freshness.STALE
    assert signals.detail == detail
    assert signals.source_at == NOW
    assert set(signals.missing_signals) == set(SIX)
    assert all(getattr(signals, name) is None for name in SIX)
    assert provider.health.state is not ProviderState.HEALTHY
    assert provider.health.last_error == detail


def test_a_changed_metric_format_drops_only_that_metric() -> None:
    clock = FrozenClock(NOW)
    changed = info_body(
        top_rat_trader_percentage={"value": "0.04"},  # a new shape
        top_bundler_trader_percentage="n/a",
        top_entrapment_trader_percentage="1.7",  # not a share
    )
    changed["data"]["total_fee"] = "-5"
    no_stat = info_body(BONK)
    del no_stat["data"]["stat"]
    api = Api(clock, info=(httpx.Response(200, json=changed), httpx.Response(200, json=no_stat)))

    (partial, statless), _ = run(api, [WIF, BONK], clock=clock)

    assert partial.freshness is Freshness.FRESH and partial.detail is None
    assert (partial.insiders_share, partial.bundling_share, partial.phishing_share) == (
        None,
        None,
        None,
    )
    assert partial.total_fees_sol is None
    assert partial.top10_holder_share == Decimal("0.323")
    assert set(partial.missing_signals) == {
        "insiders_share",
        "bundling_share",
        "phishing_share",
        "total_fees_sol",
    }
    # No ``stat`` at all: security's top-10 stays, the rest is missing, nothing raises.
    assert statless.top10_holder_share == Decimal("0.323")
    assert statless.total_fees_sol == Decimal("1223.6575276103256")
    assert {"insiders_share", "bundling_share", "phishing_share"} <= set(statless.missing_signals)


# --- the key, the plan and the limits ---------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            error(401, {"code": 401, "error": "AUTH_KEY_INVALID", "message": "x"}),
            "AUTH_KEY_INVALID",
        ),
        (
            error(
                401,
                {"code": 401, "error": "AUTH_TIMESTAMP_EXPIRED", "message": "timestamp expired"},
            ),
            "AUTH_TIMESTAMP_EXPIRED",
        ),
        (error(403, {"code": 403, "error": "AUTH_IP_BLOCKED"}), "AUTH_IP_BLOCKED"),
        (
            error(
                403,
                {
                    "code": 403,
                    "error": "PLAN_UPGRADE_REQUIRED",
                    "message": "plan",
                    "upgrade_url": "https://gmgn.ai/ai?chain=bsc&tab=paid_plans",
                    "upgrade_message": "upgrade",
                },
            ),
            "PLAN_UPGRADE_REQUIRED",
        ),
        # Over IPv6 the API answers 401/403 without its envelope (Cloudflare page).
        (error(403, "<html>Forbidden</html>"), "HTTP_403"),
    ],
    ids=["key", "timestamp", "ip", "plan", "ipv6-like"],
)
def test_a_refused_key_or_plan_is_unavailable_with_the_reason_and_never_retried(
    answer: httpx.Response, reason: str
) -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(answer,))

    readings, provider = run(api, [WIF, BONK, timedelta(minutes=5), WIF], clock=clock)

    assert api.paths == [INFO]
    assert [item.detail for item in readings] == [reason] * 3
    assert all(item.freshness is Freshness.STALE for item in readings)
    assert provider.health.state is not ProviderState.HEALTHY
    assert provider.health.last_error == reason


def test_a_refused_key_is_probed_again_only_after_the_cooldown() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(error(401, {"code": 401, "error": "AUTH_KEY_INVALID"}), default_info))

    readings, _ = run(
        api, [WIF, timedelta(minutes=14), WIF, timedelta(minutes=2), WIF], clock=clock
    )

    assert api.paths == [INFO, INFO, SECURITY]
    assert [item.detail for item in readings] == ["AUTH_KEY_INVALID", "AUTH_KEY_INVALID", None]


def reset_in(seconds: int) -> str:
    return str(int(NOW.timestamp()) + seconds)


@pytest.mark.parametrize(
    ("headers", "body", "reason", "reset_after"),
    [
        (
            {"X-RateLimit-Reset": reset_in(20)},
            {"code": 429, "error": "RATE_LIMIT_EXCEEDED", "remaining": 0, "limit": 5, "weight": 1},
            "RATE_LIMIT_EXCEEDED",
            20,
        ),
        # The body's ban end wins over an earlier header reset (whichever is later).
        (
            {"X-RateLimit-Reset": reset_in(20)},
            {
                "code": 429,
                "error": "RATE_LIMIT_BANNED",
                "message": "...",
                "reset_at": int(reset_in(40)),
            },
            "RATE_LIMIT_BANNED",
            40,
        ),
        (
            {"X-RateLimit-Reset": reset_in(300)},
            {"code": 429, "error": "RATE_LIMIT_BANNED_MAX", "reset_at": int(reset_in(300))},
            "RATE_LIMIT_BANNED_MAX",
            300,
        ),
    ],
    ids=["exceeded", "banned", "banned-max"],
)
def test_429_waits_for_the_reset_without_a_single_request_before_it(
    headers: dict[str, str], body: dict[str, object], reason: str, reset_after: int
) -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(error(429, body, headers), default_info))

    async def scenario() -> tuple[list[EnhancedTokenSignals], list[ProviderState]]:
        async with httpx.AsyncClient(
            base_url=OPENAPI_URL, transport=httpx.MockTransport(api)
        ) as client:
            provider = GmgnOpenApiProvider(
                client, api_key=SecretStr(KEY), clock=clock, sleep=Sleeper(clock)
            )
            readings = [await provider.get_token_signals(WIF)]
            states = [provider.health.state]
            clock.advance(timedelta(seconds=reset_after - 2))
            readings.append(await provider.get_token_signals(BONK))
            states.append(provider.health.state)
            clock.advance(timedelta(seconds=4))
            readings.append(await provider.get_token_signals(BONK))
            states.append(provider.health.state)
            return readings, states

    readings, states = asyncio.run(scenario())

    # One 429, nothing until the reset (repeats would extend the ban), then work resumes.
    assert api.paths == [INFO, INFO, SECURITY]
    assert api.sent_at[1] >= NOW + timedelta(seconds=reset_after)
    assert [item.detail for item in readings] == [reason, reason, None]
    assert states == [ProviderState.OPEN, ProviderState.OPEN, ProviderState.HEALTHY]


def test_a_reset_far_away_or_missing_never_blocks_for_more_than_the_ceiling() -> None:
    clock = FrozenClock(NOW)
    far = error(429, {"code": 429, "error": "RATE_LIMIT_BANNED", "reset_at": 4102444800})
    bare = error(429, {"code": 429})
    api = Api(clock, info=(far, default_info, bare, default_info))

    readings, _ = run(
        api,
        [
            WIF,
            timedelta(minutes=16),
            WIF,
            BONK,
            timedelta(seconds=30),
            BONK,
            timedelta(minutes=2),
            BONK,
        ],
        clock=clock,
    )

    assert [item.detail for item in readings] == [
        "RATE_LIMIT_BANNED",
        None,
        "RATE_LIMITED",
        "RATE_LIMITED",
        None,
    ]


def server_bucket_overflows(sent_at: Sequence[datetime], *, rate: float, capacity: float) -> bool:
    """GMGN's leaky bucket of a plan replayed over the send times (each route weighs 1)."""

    level = 0.0
    previous: datetime | None = None
    for moment in sent_at:
        if previous is not None:
            level = max(0.0, level - (moment - previous).total_seconds() * rate)
        level += 1
        previous = moment
        if level > capacity + 1e-9:
            return True
    return False


def test_the_client_limiter_keeps_below_the_free_plan_with_route_weights() -> None:
    clock = FrozenClock(NOW)
    sleeper = Sleeper(clock)
    api = Api(clock)
    mints = [mint_number(number) for number in range(15)]

    readings, _ = run(api, mints, clock=clock, sleeper=sleeper)

    assert len(api.requests) == 30
    assert all(item.detail is None for item in readings)
    assert sleeper.slept  # it had to wait: 30 units do not fit a Free bucket at once
    assert not server_bucket_overflows(api.sent_at, rate=5, capacity=5)
    # Pacing, not starving: 30 units go out at no less than 3 per second.
    assert api.sent_at[-1] - api.sent_at[0] <= timedelta(seconds=10)


def test_the_plan_is_configurable() -> None:
    clock = FrozenClock(NOW)
    sleeper = Sleeper(clock)
    api = Api(clock)

    run(
        api,
        [mint_number(number) for number in range(15)],
        clock=clock,
        sleeper=sleeper,
        plan=GmgnPlan.PRO,
    )

    assert len(api.requests) == 30
    assert sleeper.slept == []
    assert not server_bucket_overflows(api.sent_at, rate=50, capacity=50)


def test_a_long_local_queue_answers_unavailable_without_a_request() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock)

    readings, _ = run(api, [mint_number(number) for number in range(3)], clock=clock, max_wait=0.1)

    # Two candidates fill the Free bucket's margin; the third would wait past max_wait.
    assert len(api.requests) == 4
    assert [item.detail for item in readings] == [None, None, "RATE_LIMIT_LOCAL_QUEUE"]


def test_readings_are_cached_per_token() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock)

    readings, _ = run(
        api,
        [WIF, WIF, timedelta(minutes=4), WIF, BONK, timedelta(minutes=2), WIF],
        clock=clock,
        cache_ttl=timedelta(minutes=5),
    )

    assert api.paths == [INFO, SECURITY, INFO, SECURITY, INFO, SECURITY]
    assert readings[1] == readings[0]
    assert readings[4].source_at == NOW + timedelta(minutes=6)


def test_an_unavailable_reading_is_not_cached_and_one_token_is_asked_once_at_a_time() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(error(503, {"code": 503}), default_info))

    async def scenario() -> list[EnhancedTokenSignals]:
        async with httpx.AsyncClient(
            base_url=OPENAPI_URL, transport=httpx.MockTransport(api)
        ) as client:
            provider = GmgnOpenApiProvider(
                client, api_key=SecretStr(KEY), clock=clock, sleep=Sleeper(clock)
            )
            first = await provider.get_token_signals(WIF)
            together = await asyncio.gather(*(provider.get_token_signals(WIF) for _ in range(3)))
            return [first, *together]

    readings = asyncio.run(scenario())

    assert api.paths == [INFO, INFO, SECURITY]
    assert readings[0].detail == "API_UNAVAILABLE"
    assert all(item.detail is None for item in readings[1:])


def test_repeated_transport_failures_open_the_breaker_and_stop_the_requests() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(error(503, {"code": 503}),))

    readings, provider = run(
        api, [mint_number(number) for number in range(4)], clock=clock, failure_threshold=2
    )

    assert api.paths == [INFO, INFO]
    assert [item.detail for item in readings[2:]] == ["CIRCUIT_OPEN", "CIRCUIT_OPEN"]
    assert provider.health.state is ProviderState.OPEN


# --- the secret -------------------------------------------------------------------------


def test_the_key_never_leaves_the_request_header(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FrozenClock(NOW)
    echoing = error(401, {"code": 401, "error": KEY, "message": f"unknown key {KEY}"})
    scenarios = [
        Api(clock),
        Api(clock, info=(echoing,)),
        Api(clock, info=(error(429, {"code": 429, "error": "RATE_LIMIT_BANNED", "message": KEY}),)),
        Api(clock, info=(httpx.ConnectError(f"refused {KEY}"),)),
    ]
    seen: list[str] = []
    details: list[str | None] = []
    for api in scenarios:
        readings, provider = run(api, [WIF, BONK], clock=clock)
        details.append(readings[0].detail)
        seen.extend(item.model_dump_json() for item in readings)
        seen.extend(repr(item) for item in readings)
        seen.extend((repr(provider), str(provider.health)))

    seen.extend((caplog.text, *capsys.readouterr()))
    assert all(KEY not in text for text in seen)
    # A server-chosen error string is shown only when it looks like an error code.
    assert details == [None, "HTTP_401", "RATE_LIMIT_BANNED", "API_UNAVAILABLE"]


def test_settings_read_the_key_or_its_file_and_never_show_it(tmp_path: Path) -> None:
    key_file = tmp_path / "gmgn-key"
    key_file.write_text(f"{KEY}\n", encoding="utf-8")
    empty_file = tmp_path / "empty"
    empty_file.write_text("\n", encoding="utf-8")

    direct = OpenApiSettings.from_environment({"GMGN_API_KEY": KEY})
    from_file = OpenApiSettings.from_environment(
        {
            "GMGN_API_KEY_FILE": str(key_file),
            "GMGN_API_URL": "https://gmgn-proxy.internal",
            "GMGN_API_PLAN": "Plus",
            "GMGN_API_CACHE_SECONDS": "120",
        }
    )

    assert OpenApiSettings.from_environment({}) is None
    assert OpenApiSettings.from_environment({"GMGN_API_KEY": "", "GMGN_URL": "http://x"}) is None
    assert direct is not None and from_file is not None
    assert direct.api_key.get_secret_value() == KEY
    assert (direct.base_url, direct.plan, direct.cache_ttl) == (
        OPENAPI_URL,
        GmgnPlan.FREE,
        timedelta(minutes=5),
    )
    assert from_file.api_key.get_secret_value() == KEY
    assert (from_file.base_url, from_file.plan, from_file.cache_ttl) == (
        "https://gmgn-proxy.internal",
        GmgnPlan.PLUS,
        timedelta(seconds=120),
    )
    assert KEY not in repr(direct) and KEY not in str(from_file)
    for environ in (
        {"GMGN_API_KEY": KEY, "GMGN_API_KEY_FILE": str(key_file)},
        {"GMGN_API_KEY_FILE": str(empty_file)},
        {"GMGN_API_KEY": KEY, "GMGN_API_PLAN": "enterprise"},
        {"GMGN_API_KEY": KEY, "GMGN_API_CACHE_SECONDS": "0"},
    ):
        with pytest.raises(ValueError) as raised:
            OpenApiSettings.from_environment(environ)
        assert KEY not in str(raised.value)


def test_requests_leave_over_ipv4_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPv6 gets 401/403 from GMGN: the transport binds an IPv4 source address."""

    built: list[dict[str, object]] = []
    original = httpx.AsyncHTTPTransport

    def recording(**options: Any) -> httpx.AsyncHTTPTransport:
        built.append(options)
        return original(**options)

    monkeypatch.setattr(openapi.httpx, "AsyncHTTPTransport", recording)
    settings = OpenApiSettings(
        api_key=SecretStr(KEY), base_url=OPENAPI_URL, plan=GmgnPlan.FREE, cache_ttl=timedelta(1)
    )

    ipv4_transport()
    client = openapi_client(settings, timeout=5)
    asyncio.run(client.aclose())

    assert len(built) == 2
    assert all(options["local_address"] == "0.0.0.0" for options in built)  # noqa: S104
    assert str(client.base_url).rstrip("/") == OPENAPI_URL
    assert API_KEY_HEADER not in client.headers  # the key goes per request, never as a default
