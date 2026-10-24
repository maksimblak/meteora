"""gmgn.ai's own web API as the browser bridge reads it (G25): requests, refusals, signals.

Addresses (reconnaissance of 2026-10-24, values checked against the token page; answers
``{"code": 0, "message": "success", "data": ...}``):

* ``POST /api/v1/mutil_window_token_info`` ``{"chain", "addresses": [...]}`` - a batch, one
  item per address: ``price.price``, ``price.volume_5m``/``volume_1h`` (USD),
  ``circulating_supply``, ``total_fee`` (SOL, the page's "Total Fees"), ``creation_timestamp``
  (Unix seconds), ``dev.creator_token_status`` (``creator_close`` seen), ``dev.top_10_holder_rate``;
* ``GET /api/v1/token_stat/{chain}/{mint}`` - fractions as decimal strings: insiders
  ``top_rat_trader_percentage``, bundling ``top_bundler_trader_percentage``, phishing
  ``top_entrapment_trader_percentage``, ``top_10_holder_rate``, developer ``creator_hold_rate``
  and ``dev_team_hold_rate``, snipers' holding ``top70_sniper_hold_rate``;
* ``GET /vas/api/v1/token_holder_stat/{chain}/{mint}`` - counters, ``sniper_count`` (they stop
  at 1000);
* ``GET /api/v1/token_fee_info/{chain}/{mint}`` - ``security.top_10_holder_rate`` (fallback);
* ``GET /api/v1/token_mcap_candles/{chain}/{mint}?pool_type=tpool&resolution=1d`` - daily
  market-cap candles; the ATH market cap is the highest ``high`` (``1m`` covers only hours);
* ``POST /rrs/api/v1/trending_multichain`` - Trending, short keys: ``a`` address, ``iwt`` wash
  trading. A token outside Trending has no verdict (``None``), never a clean one.

Every answer that is not a success about the token asked for is a ``Refusal``: a Cloudflare
challenge, a block or a rate limit pauses the bridge (``pause``) and is never worked around;
a login wall is ``LOGIN_REQUIRED`` - only the user can sign in, in the bridge window.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from gmgn_service.contract import V2_SIGNAL_NAMES
from gmgn_service.extract import amount, exact_number, fraction, parse_body

TOKEN_INFO = "mutil_window_token_info"  # noqa: S105 - an endpoint name
TOKEN_STAT = "token_stat"  # noqa: S105 - an endpoint name
HOLDER_STAT = "token_holder_stat"
FEE_INFO = "token_fee_info"
MCAP_CANDLES = "token_mcap_candles"
TRENDING = "trending_multichain"

_CHALLENGE_MARKS = ("just a moment", "challenges.cloudflare.com", "checking your browser")
_BLOCK_MARKS = ("you have been blocked", "access denied", "attention required")
_LOGIN_MARKS = ("login", "log in", "sign in", "unauthori", "authenticat")


@dataclass(frozen=True)
class WebAnswer:
    """One answer of a ``fetch`` from the gmgn.ai tab; ``status`` 0 = the fetch itself failed
    (``timed_out``: it was cut off after the request timeout)."""

    status: int
    content_type: str
    text: str
    timed_out: bool = False


@dataclass(frozen=True)
class Refusal:
    reason: str
    pause: bool


@dataclass(frozen=True)
class WebRequest:
    name: str
    method: str
    path: str
    body: str | None = None


@dataclass(frozen=True)
class BridgeReading:
    """The v2 signals of one token (``None`` = not reported) and the answers they came from."""

    signals: dict[str, object]
    sources: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in V2_SIGNAL_NAMES if self.signals.get(name) is None)

    @property
    def empty(self) -> bool:
        return len(self.missing) == len(V2_SIGNAL_NAMES)


def answer_data(answer: WebAnswer) -> Any:
    """The ``data`` of a successful answer, or the ``Refusal`` that says why there is none."""

    lowered = answer.text[:4000].lower()
    status = answer.status
    if status == 0:
        return Refusal("API_TIMEOUT" if answer.timed_out else "NETWORK_ERROR", False)
    if status == 429:
        return Refusal("RATE_LIMITED", True)
    if status == 403:
        challenge = any(mark in lowered for mark in _CHALLENGE_MARKS)
        return Refusal("CHALLENGE" if challenge else "ACCESS_BLOCKED", True)
    if status == 401:
        return Refusal("LOGIN_REQUIRED", False)
    if status >= 500:
        return Refusal("API_UNAVAILABLE", False)
    if status != 200:
        return Refusal("API_ERROR", False)
    if any(mark in lowered for mark in _CHALLENGE_MARKS):
        return Refusal("CHALLENGE", True)
    if "json" not in answer.content_type.lower() and any(mark in lowered for mark in _BLOCK_MARKS):
        return Refusal("ACCESS_BLOCKED", True)
    body = parse_body(answer.text)
    if not isinstance(body, Mapping) or "code" not in body:
        return Refusal("API_PAYLOAD_INVALID", False)
    code = body["code"]
    if isinstance(code, bool) or code != 0:
        message = str(body.get("message") or body.get("reason") or "").lower()
        if any(mark in message for mark in _LOGIN_MARKS):
            return Refusal("LOGIN_REQUIRED", False)
        return Refusal("API_ERROR", False)
    return body.get("data")


def info_request(chain: str, mints: Sequence[str]) -> WebRequest:
    body = json.dumps({"chain": chain, "addresses": list(mints)})
    return WebRequest(TOKEN_INFO, "POST", f"/api/v1/{TOKEN_INFO}", body)


def token_requests(chain: str, mint: str) -> tuple[WebRequest, ...]:
    return (
        WebRequest(TOKEN_STAT, "GET", f"/api/v1/{TOKEN_STAT}/{chain}/{mint}"),
        WebRequest(HOLDER_STAT, "GET", f"/vas/api/v1/{HOLDER_STAT}/{chain}/{mint}"),
        WebRequest(FEE_INFO, "GET", f"/api/v1/{FEE_INFO}/{chain}/{mint}"),
        WebRequest(
            MCAP_CANDLES,
            "GET",
            f"/api/v1/{MCAP_CANDLES}/{chain}/{mint}?pool_type=tpool&resolution=1d&limit=500",
        ),
    )


def trending_request(chain: str) -> WebRequest:
    """Trending of the last hour, as the page asks for it (without its launchpad filter)."""

    body = {"params": [{"chain": chain, "interval": "1h", "filter": {"limit": 500}}]}
    return WebRequest(TRENDING, "POST", f"/rrs/api/v1/{TRENDING}", json.dumps(body))


def token_info_items(data: Any) -> dict[str, Mapping[str, Any]]:
    """The batch answer by address; anything without an address is dropped."""

    items = data if isinstance(data, list) else [data]
    return {
        item["address"]: item
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("address"), str)
    }


def trending_wash(data: Any) -> dict[str, bool]:
    """``iwt`` of every Trending row by address; a non-boolean verdict is not taken."""

    blocks = data if isinstance(data, list) else [data]
    verdicts: dict[str, bool] = {}
    for block in blocks:
        rows = block.get("tokens") if isinstance(block, Mapping) else None
        for row in rows if isinstance(rows, list) else []:
            if (
                isinstance(row, Mapping)
                and isinstance(row.get("a"), str)
                and isinstance(row.get("iwt"), bool)
            ):
                verdicts[row["a"]] = row["iwt"]
    return verdicts


# ``creator_token_status``: ``creator_close`` was seen on the page (2026-10-24); the other
# words are those of the official OpenAPI for the same field.
_DEVELOPER_STATUSES = {
    "creator_hold": "HOLDING",
    "hold": "HOLDING",
    "creator_close": "SOLD",
    "creator_sell": "SOLD",
    "sell": "SOLD",
    "close": "SOLD",
}


def bridge_signals(
    mint: str,
    *,
    info: Mapping[str, Any] | None,
    stat: Mapping[str, Any] | None,
    holder_stat: Mapping[str, Any] | None,
    fee_info: Mapping[str, Any] | None,
    candles: Mapping[str, Any] | None,
    wash: Mapping[str, bool],
) -> BridgeReading:
    """The v2 signals of ``mint`` from the answers read for it (``None`` = not read).

    An answer about another token, or a field in a new shape, gives nothing - a changed
    format shows up as a missing signal, never as zero.
    """

    sources: set[str] = set()
    info = _about(info, mint, sources, TOKEN_INFO)
    fee_info = _about(fee_info, mint, sources, FEE_INFO)
    stat = _taken(stat, sources, TOKEN_STAT)
    holder_stat = _taken(holder_stat, sources, HOLDER_STAT)
    candles = _taken(candles, sources, MCAP_CANDLES)
    if mint in wash:
        sources.add(TRENDING)
    info_map = info or {}
    stat_map = stat or {}
    dev = _section(info_map, "dev")
    prices = _section(info_map, "price")
    security = _section(fee_info or {}, "security")

    developer = [
        value
        for value in (
            fraction(stat_map.get("creator_hold_rate")),
            fraction(stat_map.get("dev_team_hold_rate")),
        )
        if value is not None
    ]
    price = _positive(prices.get("price"))
    supply = _positive(info_map.get("circulating_supply"))
    market_cap = None if price is None or supply is None else price * supply
    ath_market_cap = _ath(candles, market_cap)
    status = dev.get("creator_token_status")
    signals: dict[str, object] = {
        "top10_holder_share": _first(
            fraction(stat_map.get("top_10_holder_rate")),
            fraction(dev.get("top_10_holder_rate")),
            fraction(security.get("top_10_holder_rate")),
        ),
        "insiders_share": fraction(stat_map.get("top_rat_trader_percentage")),
        "bundling_share": fraction(stat_map.get("top_bundler_trader_percentage")),
        "phishing_share": fraction(stat_map.get("top_entrapment_trader_percentage")),
        "developer_share": max(developer) if developer else None,
        "total_fees_sol": amount(info_map.get("total_fee")),
        "volume_5m_usd": amount(prices.get("volume_5m")),
        "volume_1h_usd": amount(prices.get("volume_1h")),
        "price_usd": price,
        "market_cap_usd": market_cap,
        "ath_price_usd": None
        if ath_market_cap is None or supply is None
        else ath_market_cap / supply,
        "ath_market_cap_usd": ath_market_cap,
        "sniper_count": _count((holder_stat or {}).get("sniper_count")),
        "sniper_share": fraction(stat_map.get("top70_sniper_hold_rate")),
        "developer_status": _DEVELOPER_STATUSES.get(status.strip().lower())
        if isinstance(status, str)
        else None,
        "created_at": _unix(info_map.get("creation_timestamp"))
        or _unix(info_map.get("open_timestamp")),
        "wash_trading": wash.get(mint),
    }
    return BridgeReading(signals=signals, sources=tuple(sorted(sources)))


def _about(
    answer: Mapping[str, Any] | None, mint: str, sources: set[str], name: str
) -> Mapping[str, Any] | None:
    if not isinstance(answer, Mapping) or answer.get("address") != mint:
        return None
    sources.add(name)
    return answer


def _taken(
    answer: Mapping[str, Any] | None, sources: set[str], name: str
) -> Mapping[str, Any] | None:
    if not isinstance(answer, Mapping):
        return None
    sources.add(name)
    return answer


def _section(answer: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = answer.get(name)
    return section if isinstance(section, Mapping) else {}


def _ath(candles: Mapping[str, Any] | None, market_cap: Decimal | None) -> Decimal | None:
    rows = (candles or {}).get("list")
    highs = [
        value
        for value in (
            _positive(row.get("high")) for row in _mappings(rows if isinstance(rows, list) else [])
        )
        if value is not None
    ]
    if not highs:
        return None
    # The cap right now is an all-time high too, if the daily candle has not caught up yet.
    return max(highs if market_cap is None else [*highs, market_cap])


def _mappings(items: Iterable[object]) -> Iterable[Mapping[str, Any]]:
    return (item for item in items if isinstance(item, Mapping))


def _positive(value: object) -> Decimal | None:
    number = exact_number(value)
    return number if number is not None and number > 0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _unix(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _first(*values: Decimal | None) -> Decimal | None:
    return next((value for value in values if value is not None), None)
