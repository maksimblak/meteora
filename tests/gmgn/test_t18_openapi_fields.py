"""T18: what the official GMGN OpenAPI adds to ``EnhancedTokenSignals`` for selection and exits.

``openapi/token_info_pumptache.json`` and ``openapi/token_security_pumptache.json`` are live
answers of ``GET /v1/token/info`` and ``GET /v1/token/security`` for a trending pump.fun token
(2026-10-24, recorded once with the public demo key of GMGNAI/gmgn-skills), trimmed to the
fields around the ones read: no links, logos or wallets of people. Every expected value below
is read off those files by hand. No test here goes to the network.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import DeveloperStatus, RiskSignal
from tests.gmgn.test_gmgn_openapi import NOW, WIF, Api, asked, run

FIXTURES = Path(__file__).parent / "openapi"
HOT = PublicKey("95MV9VPQC7pBZRRS9t8CJvBCi4J3VEH37ZJSGAWapump")


def recorded(kind: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(
        (FIXTURES / f"token_{kind}_pumptache.json").read_text(encoding="utf-8")
    )
    return body


def hot_api(clock: FrozenClock, *, info: Any = None, security: Any = None) -> Api:
    info_body = info or recorded("info")
    security_body = security or recorded("security")
    return Api(
        clock,
        info=(lambda request: httpx.Response(200, json=info_body),),
        security=(lambda request: httpx.Response(200, json=security_body),),
    )


def test_the_live_answer_gives_token_volume_cap_ath_snipers_and_developer_status() -> None:
    clock = FrozenClock(NOW)

    (signals,), _ = run(hot_api(clock), [HOT], clock=clock)

    circulating = Decimal("984782266.081891")
    # ``price.volume_5m`` / ``price.volume_1h``: USD over every pool of the token.
    assert signals.volume_5m_usd == Decimal("130647.22660063")
    assert signals.volume_1h_usd == Decimal("2173116.31290007")
    # Market cap is not served: ``price.price`` x ``circulating_supply`` (gmgn-token SKILL.md).
    assert signals.price_usd == Decimal("0.00093852412")
    assert signals.market_cap_usd == Decimal("0.00093852412") * circulating
    assert signals.ath_price_usd == Decimal("0.0011828344")
    assert signals.ath_market_cap_usd == Decimal("0.0011828344") * circulating
    # ``wallet_tags_stat.sniper_wallets`` and ``stat.top70_sniper_hold_rate``.
    assert signals.sniper_count == 1
    assert signals.sniper_share == Decimal("0.000786029")
    # ``dev.creator_token_status: creator_hold``; the share is the largest one reported.
    assert signals.developer_status is DeveloperStatus.HOLDING
    assert signals.developer_share == Decimal("0.0008")
    assert signals.created_at == datetime.fromtimestamp(1792834591, UTC)
    # The live security answer carries no wash-trading verdict: unknown, not "clean".
    assert signals.wash_trading is RiskSignal.UNKNOWN


def test_the_wif_answer_reports_a_creator_who_closed_and_capped_sniper_wallets() -> None:
    clock = FrozenClock(NOW)

    (signals,), _ = run(Api(clock), [WIF], clock=clock)

    assert signals.developer_status is DeveloperStatus.SOLD  # ``creator_close``
    assert signals.sniper_count == 1000
    assert signals.ath_price_usd == Decimal("4.8499674634069185")
    assert signals.volume_1h_usd == Decimal("1732.89887649")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("creator_hold", DeveloperStatus.HOLDING),
        ("hold", DeveloperStatus.HOLDING),
        ("creator_close", DeveloperStatus.SOLD),
        ("sell", DeveloperStatus.SOLD),
        ("", None),
        ("something_new", None),
    ],
)
def test_both_vocabularies_of_the_creator_status_are_understood(
    raw: str, expected: DeveloperStatus | None
) -> None:
    clock = FrozenClock(NOW)
    info = recorded("info")
    info["data"]["dev"]["creator_token_status"] = raw

    (signals,), _ = run(hot_api(clock, info=info), [HOT], clock=clock)

    assert signals.developer_status is expected


@pytest.mark.parametrize(
    ("flag", "expected"), [(True, RiskSignal.UNSAFE), (False, RiskSignal.SAFE)]
)
def test_a_wash_trading_verdict_of_the_security_answer_is_kept(
    flag: bool, expected: RiskSignal
) -> None:
    clock = FrozenClock(NOW)
    security = recorded("security")
    security["data"]["is_wash_trading"] = flag

    (signals,), _ = run(hot_api(clock, security=security), [HOT], clock=clock)

    assert signals.wash_trading is expected


def test_a_broken_volume_or_price_is_left_unknown_without_losing_the_reading() -> None:
    clock = FrozenClock(NOW)
    info = recorded("info")
    info["data"]["price"]["volume_1h"] = "not-a-number"
    info["data"]["price"]["price"] = "-1"
    del info["data"]["wallet_tags_stat"]

    (signals,), _ = run(hot_api(clock, info=info), [HOT], clock=clock)

    assert signals.volume_1h_usd is None
    assert signals.price_usd is None and signals.market_cap_usd is None
    assert signals.sniper_count is None
    assert signals.volume_5m_usd == Decimal("130647.22660063")
    assert signals.detail is None and signals.insiders_share == Decimal("0.015")


def test_requests_and_their_weights_stay_the_two_routes_of_t16() -> None:
    clock = FrozenClock(NOW)
    api = hot_api(clock)

    run(api, [HOT], clock=clock)

    assert api.paths == ["/v1/token/info", "/v1/token/security"]
    assert {asked(request) for request in api.requests} == {HOT}
