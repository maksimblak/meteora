"""``gmgn-signals.v1``: the one answer of ``GET /v1/tokens/{chain}/{mint}/signals``.

Shares are fractions in [0, 1] and ``total_fees_sol`` is SOL, all as exact decimal strings
(``null`` = not reported). ``AVAILABLE`` carries page data from ``source_at``, ``FRESH``
while younger than the cache TTL; ``UNAVAILABLE`` carries none and ``reason`` says why.
Reasons: ``ACCESS_BLOCKED``, ``CAPTCHA``, ``CHALLENGE_TIMEOUT``, ``PAGE_TIMEOUT``,
``API_BLOCKED``, ``RATE_LIMITED``, ``NO_SIGNALS``, ``NETWORK_ERROR``, ``BROWSER_ERROR``,
``BROWSER_UNAVAILABLE``, ``FETCH_PENDING``, ``QUEUE_FULL``, ``CIRCUIT_OPEN``.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "gmgn-signals.v1"


class SignalValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    top10_holder_share: Decimal | None = None
    insiders_share: Decimal | None = None
    bundling_share: Decimal | None = None
    phishing_share: Decimal | None = None
    developer_share: Decimal | None = None
    total_fees_sol: Decimal | None = None


class SignalsV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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
