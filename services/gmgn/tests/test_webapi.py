"""Reading gmgn.ai's own web API answers (G25): what each answer means and what it gives.

The fixture is a live recording of 2026-10-24 (``fixtures/bridge_grok.json``): the five
addresses of the reconnaissance and Trending, fetched from a visible Playwright Chromium tab
on a clean profile without an account. Expected values are the recorded fields themselves.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gmgn_service.contract import V2_SIGNAL_NAMES
from gmgn_service.webapi import (
    Refusal,
    WebAnswer,
    answer_data,
    bridge_signals,
    info_request,
    token_info_items,
    token_requests,
    trending_request,
    trending_wash,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "bridge_grok.json").read_text(encoding="utf-8")
)
MINT = FIXTURE["mint"]
OTHER = "CHeF6oKrec7pMDo1HcRhVmNXjKkJtaphpRhmiDnHpump"
CHALLENGE_PAGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


def ok(name: str) -> WebAnswer:
    return WebAnswer(200, "application/json; charset=utf-8", json.dumps(FIXTURE["answers"][name]))


def data(name: str) -> Any:
    value = answer_data(ok(name))
    assert not isinstance(value, Refusal)
    return value


def recorded(**changes: Any) -> dict[str, Any]:
    parts: dict[str, Any] = {
        "info": token_info_items(data("mutil_window_token_info")).get(MINT),
        "stat": data("token_stat"),
        "holder_stat": data("token_holder_stat"),
        "fee_info": data("token_fee_info"),
        "candles": data("token_mcap_candles"),
        "wash": trending_wash(data("trending_multichain")),
    }
    parts.update(changes)
    return parts


def test_the_recorded_answers_give_every_v2_signal_of_the_token() -> None:
    reading = bridge_signals(MINT, **recorded())
    supply = Decimal("995387904.629032")

    assert reading.signals == {
        "top10_holder_share": Decimal("0.0443"),
        "insiders_share": Decimal("0.7968"),
        "bundling_share": Decimal("0.0325"),
        "phishing_share": Decimal("0.7968"),
        # the larger of creator_hold_rate 0 and dev_team_hold_rate - never under-reported
        "developer_share": Decimal("0.0325267424"),
        "total_fees_sol": Decimal("0.9718177443108398"),
        "volume_5m_usd": Decimal("7419.35651281"),
        "volume_1h_usd": Decimal("137356.48892064"),
        "price_usd": Decimal("0.00016726742"),
        "market_cap_usd": Decimal("0.00016726742") * supply,
        "ath_price_usd": Decimal("802775.7165") / supply,
        "ath_market_cap_usd": Decimal("802775.7165"),
        "sniper_count": 20,
        "sniper_share": Decimal("0.0325267932"),
        "developer_status": "SOLD",  # creator_token_status "creator_close"
        "created_at": datetime(2026, 10, 24, 15, 56, 15, tzinfo=UTC),
        "wash_trading": False,
    }
    assert reading.missing == ()
    assert reading.sources == (
        "mutil_window_token_info",
        "token_fee_info",
        "token_holder_stat",
        "token_mcap_candles",
        "token_stat",
        "trending_multichain",
    )
    assert tuple(reading.signals) == V2_SIGNAL_NAMES


def test_a_token_outside_trending_has_an_unknown_wash_trading_verdict_not_a_clean_one() -> None:
    reading = bridge_signals(MINT, **recorded(wash={}))

    assert reading.signals["wash_trading"] is None
    assert reading.missing == ("wash_trading",)


def test_answers_about_another_token_or_in_a_new_format_are_not_taken() -> None:
    info = dict(token_info_items(data("mutil_window_token_info"))[MINT])
    info["price"] = {**info["price"], "volume_5m": 7419.35, "volume_1h": "-1"}
    info["dev"] = {**info["dev"], "creator_token_status": "creator_unknown_new"}
    stat = {**data("token_stat"), "top_rat_trader_percentage": "79.68"}  # not a fraction
    fee_info = {**data("token_fee_info"), "address": OTHER}

    reading = bridge_signals(
        MINT, **recorded(info=info, stat=stat, fee_info=fee_info, holder_stat=None)
    )

    assert reading.signals["volume_5m_usd"] is None  # a binary float is never trusted
    assert reading.signals["volume_1h_usd"] is None
    assert reading.signals["developer_status"] is None
    assert reading.signals["insiders_share"] is None
    assert reading.signals["sniper_count"] is None
    assert reading.signals["price_usd"] == Decimal("0.00016726742")
    assert set(reading.missing) == {
        "volume_5m_usd",
        "volume_1h_usd",
        "developer_status",
        "insiders_share",
        "sniper_count",
    }


def test_nothing_read_is_an_empty_reading() -> None:
    reading = bridge_signals(
        MINT, info=None, stat=None, holder_stat=None, fee_info=None, candles=None, wash={}
    )

    assert reading.empty
    assert reading.missing == V2_SIGNAL_NAMES


def test_the_batch_info_answer_is_split_by_address() -> None:
    items = token_info_items(data("mutil_window_token_info"))

    assert set(items) == {MINT}


@pytest.mark.parametrize(
    ("answer", "reason", "pause"),
    [
        (WebAnswer(429, "text/html; charset=UTF-8", CHALLENGE_PAGE), "RATE_LIMITED", True),
        (WebAnswer(403, "text/html; charset=UTF-8", CHALLENGE_PAGE), "CHALLENGE", True),
        (WebAnswer(200, "text/html; charset=UTF-8", CHALLENGE_PAGE), "CHALLENGE", True),
        (
            WebAnswer(403, "text/html", "<html>Sorry, you have been blocked</html>"),
            "ACCESS_BLOCKED",
            True,
        ),
        (
            WebAnswer(401, "application/json", '{"code":401,"message":"unauthorized"}'),
            "LOGIN_REQUIRED",
            False,
        ),
        (
            WebAnswer(200, "application/json", '{"code":40100,"message":"please login first"}'),
            "LOGIN_REQUIRED",
            False,
        ),
        (
            WebAnswer(200, "application/json", '{"code":5,"message":"param error"}'),
            "API_ERROR",
            False,
        ),
        (WebAnswer(502, "text/html", "<html>Bad gateway</html>"), "API_UNAVAILABLE", False),
        (WebAnswer(200, "text/plain", "not json"), "API_PAYLOAD_INVALID", False),
        (WebAnswer(0, "", ""), "NETWORK_ERROR", False),
    ],
    ids=[
        "429",
        "403-challenge",
        "200-challenge",
        "403-block",
        "401",
        "login-code",
        "code",
        "502",
        "text",
        "network",
    ],
)
def test_every_refusal_has_its_reason_and_only_a_block_pauses(
    answer: WebAnswer, reason: str, pause: bool
) -> None:
    assert answer_data(answer) == Refusal(reason, pause)


def test_the_requests_are_the_addresses_of_the_reconnaissance() -> None:
    batch = info_request("sol", [MINT, OTHER])
    paths = [(item.name, item.method, item.path) for item in token_requests("sol", MINT)]
    trending = trending_request("sol")

    assert (batch.method, batch.path) == ("POST", "/api/v1/mutil_window_token_info")
    assert json.loads(batch.body or "") == {"chain": "sol", "addresses": [MINT, OTHER]}
    assert paths == [
        ("token_stat", "GET", f"/api/v1/token_stat/sol/{MINT}"),
        ("token_holder_stat", "GET", f"/vas/api/v1/token_holder_stat/sol/{MINT}"),
        ("token_fee_info", "GET", f"/api/v1/token_fee_info/sol/{MINT}"),
        (
            "token_mcap_candles",
            "GET",
            f"/api/v1/token_mcap_candles/sol/{MINT}?pool_type=tpool&resolution=1d&limit=500",
        ),
    ]
    assert (trending.method, trending.path) == ("POST", "/rrs/api/v1/trending_multichain")
    assert json.loads(trending.body or "")["params"][0]["chain"] == "sol"
