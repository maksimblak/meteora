"""``gmgn-signals.v1`` and ``.v2``: the answer of ``GET /v1/tokens/{chain}/{mint}/signals``.

Shares are fractions in [0, 1] and ``total_fees_sol`` is SOL, all as exact decimal strings
(``null`` = not reported). ``AVAILABLE`` carries page data from ``source_at``, ``FRESH``
while younger than the cache TTL; ``UNAVAILABLE`` carries none and ``reason`` says why.

v1 (the headless page service, G18) reasons: ``ACCESS_BLOCKED``, ``CAPTCHA``,
``CHALLENGE_TIMEOUT``, ``PAGE_TIMEOUT``, ``API_BLOCKED``, ``RATE_LIMITED``, ``NO_SIGNALS``,
``NETWORK_ERROR``, ``BROWSER_ERROR``, ``BROWSER_UNAVAILABLE``, ``FETCH_PENDING``,
``QUEUE_FULL``, ``CIRCUIT_OPEN``.

v2 (the browser bridge, G25) = v1 plus the token's market figures, the same ones the worker
reads from the official OpenAPI: ``volume_5m_usd``/``volume_1h_usd``, ``price_usd``,
``market_cap_usd`` (price x circulating supply), ``ath_price_usd``/``ath_market_cap_usd``,
``sniper_count`` (an integer) and ``sniper_share``, ``developer_status`` (``HOLDING`` or
``SOLD``), ``created_at`` (ISO 8601, UTC) and ``wash_trading`` (``true``/``false``; ``null`` =
unknown, never "clean"). Its own reasons besides those of v1: ``CHALLENGE``,
``LOGIN_REQUIRED``, ``API_ERROR``, ``API_TIMEOUT``, ``API_UNAVAILABLE``,
``API_PAYLOAD_INVALID``, ``BROWSER_CLOSED``.
"""

from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "gmgn-signals.v1"
SCHEMA_VERSION_V2 = "gmgn-signals.v2"


class SignalValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    top10_holder_share: Decimal | None = None
    insiders_share: Decimal | None = None
    bundling_share: Decimal | None = None
    phishing_share: Decimal | None = None
    developer_share: Decimal | None = None
    total_fees_sol: Decimal | None = None


class SignalValuesV2(SignalValues):
    volume_5m_usd: Decimal | None = None
    volume_1h_usd: Decimal | None = None
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    ath_price_usd: Decimal | None = None
    ath_market_cap_usd: Decimal | None = None
    sniper_count: int | None = None
    sniper_share: Decimal | None = None
    developer_status: Literal["HOLDING", "SOLD"] | None = None
    created_at: datetime | None = None
    wash_trading: bool | None = None


V1_SIGNAL_NAMES = tuple(SignalValues.model_fields)
V2_SIGNAL_NAMES = tuple(SignalValuesV2.model_fields)


class SignalsV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_names: ClassVar[tuple[str, ...]] = V1_SIGNAL_NAMES

    schema_version: Literal["gmgn-signals.v1"] = "gmgn-signals.v1"
    chain: str
    mint: str
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reason: str | None = None
    source_at: datetime | None = None
    freshness: Literal["FRESH", "STALE"]
    signals: SignalValues = SignalValues()
    missing: list[str]
    sources: list[str] = []


class SignalsV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_names: ClassVar[tuple[str, ...]] = V2_SIGNAL_NAMES

    schema_version: Literal["gmgn-signals.v2"] = "gmgn-signals.v2"
    chain: str
    mint: str
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reason: str | None = None
    source_at: datetime | None = None
    freshness: Literal["FRESH", "STALE"]
    signals: SignalValuesV2 = SignalValuesV2()
    missing: list[str]
    sources: list[str] = []


Signals = SignalsV1 | SignalsV2
