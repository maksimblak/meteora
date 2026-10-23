"""``GmgnProvider``: the enhanced-signal provider of the worker backed by the ``gmgn`` service.

The service (``services/gmgn``) opens the public token page on gmgn.ai in a real headless
browser and answers ``GET /v1/tokens/{chain}/{mint}/signals`` with normalized shares
(schema ``gmgn-signals.v1``). This client turns that answer into ``EnhancedTokenSignals``.

Nothing here raises into the candidate pipeline: an unreachable service, an open breaker,
a page behind a challenge, an unknown schema or a malformed field all end in a reading
that is ``STALE`` with ``detail`` naming the cause, or in a fresh reading without the
metric that changed its format. Fresh means the page data are at most ``stale_after`` old.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.market.models import EnhancedTokenSignals, Freshness, ProviderHealth, ProviderState
from src.providers.contracts import (
    ProviderPayloadError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from src.providers.resilience import CircuitBreaker, ResilientHttpClient, RetryPolicy

SCHEMA_VERSION = "gmgn-signals.v1"
PROVIDER = "gmgn"
# Fractions in [0, 1] and the non-negative SOL amount, as the service names them.
SHARE_SIGNALS = (
    "top10_holder_share",
    "insiders_share",
    "bundling_share",
    "phishing_share",
    "developer_share",
)
AMOUNT_SIGNALS = ("total_fees_sol",)


class GmgnProvider:
    """``get_token_signals(mint) -> EnhancedTokenSignals`` and normalized ``health``.

    One attempt per call: the service caches, backs off and breaks its own circuit, so the
    worker never repeats a page load. ``failure_threshold`` failed calls in a row open the
    worker-side breaker for ``recovery_timeout`` seconds; meanwhile no request is sent.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock,
        chain: str = "sol",
        stale_after: timedelta = timedelta(minutes=15),
        timeout: float = 20,
        failure_threshold: int = 3,
        recovery_timeout: float = 60,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self._clock = clock
        self._chain = chain
        self._stale_after = stale_after
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold, recovery_timeout=recovery_timeout
        )
        self._http = ResilientHttpClient(
            client,
            retry_policy=RetryPolicy(max_attempts=1),
            circuit_breaker=self._breaker,
            timeout=timeout,
        )
        self._unavailable_in_row = 0
        self._last_detail: str | None = None

    @property
    def health(self) -> ProviderHealth:
        transport = self._breaker.health
        if transport.state is not ProviderState.HEALTHY or not self._unavailable_in_row:
            return transport
        # The service answered, but without data (challenge, captcha, its own breaker).
        return ProviderHealth(
            ProviderState.DEGRADED, self._unavailable_in_row, self._last_detail, None
        )

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
        signals = await self._read(mint)
        if signals.detail is None:
            self._unavailable_in_row = 0
            self._last_detail = None
        else:
            self._unavailable_in_row += 1
            self._last_detail = signals.detail
        return signals

    async def _read(self, mint: PublicKey) -> EnhancedTokenSignals:
        try:
            self._breaker.before_request()
        except ProviderUnavailableError:
            return self._unavailable("CIRCUIT_OPEN")
        try:
            body = await self._http.get_json(f"/v1/tokens/{self._chain}/{mint.root}/signals")
        except ProviderTimeoutError:
            return self._unavailable("SERVICE_TIMEOUT")
        except ProviderPayloadError:
            return self._unavailable("SERVICE_PAYLOAD_INVALID")
        except ProviderUnavailableError:
            return self._unavailable("SERVICE_UNAVAILABLE")
        return self._parse(body, mint)

    def _parse(self, body: Any, mint: PublicKey) -> EnhancedTokenSignals:
        if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
            return self._unavailable("SERVICE_SCHEMA_UNKNOWN")
        if body.get("mint") != mint.root or body.get("chain") != self._chain:
            return self._unavailable("SERVICE_MINT_MISMATCH")
        if body.get("status") != "AVAILABLE":
            reason = body.get("reason")
            if not isinstance(reason, str) or not reason:
                reason = "UNAVAILABLE"
            return self._unavailable(reason)
        now = self._clock.now()
        source_at = _timestamp(body.get("source_at"))
        raw_signals = body.get("signals")
        if source_at is None or not isinstance(raw_signals, dict):
            return self._unavailable("SERVICE_PAYLOAD_INVALID")
        source_at = min(source_at, now)
        values: dict[str, Decimal | None] = {}
        missing: list[str] = []
        for name in (*SHARE_SIGNALS, *AMOUNT_SIGNALS):
            value = _decimal(raw_signals.get(name))
            if value is not None and (value < 0 or (name in SHARE_SIGNALS and value > Decimal(1))):
                value = None
            values[name] = value
            if value is None:
                missing.append(name)
        fresh = body.get("freshness") == Freshness.FRESH.value and (
            now - source_at <= self._stale_after
        )
        return EnhancedTokenSignals(
            top10_holder_share=values["top10_holder_share"],
            insiders_share=values["insiders_share"],
            bundling_share=values["bundling_share"],
            phishing_share=values["phishing_share"],
            developer_share=values["developer_share"],
            total_fees_sol=values["total_fees_sol"],
            missing_signals=tuple(missing),
            provider=PROVIDER,
            source_at=source_at,
            freshness=Freshness.FRESH if fresh else Freshness.STALE,
        )

    def _unavailable(self, detail: str) -> EnhancedTokenSignals:
        return EnhancedTokenSignals(
            missing_signals=(*SHARE_SIGNALS, *AMOUNT_SIGNALS),
            provider=PROVIDER,
            source_at=self._clock.now(),
            freshness=Freshness.STALE,
            detail=detail,
        )


def _decimal(value: object) -> Decimal | None:
    """An exact decimal string; a binary float or any other shape is not trusted."""

    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
