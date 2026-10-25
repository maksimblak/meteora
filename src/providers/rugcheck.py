"""``RugCheckProvider``: RugCheck's summary report of a deep candidate's token (T22).

What is read - checked against live answers for 30 memecoins of the top Meteora DLMM pools
on 2026-10-25: ``GET /v1/tokens/{mint}/report/summary``, no key, answers ``risks`` (each with
``name`` and ``level``: ``danger`` or ``warn``), ``score_normalised`` (0..100, higher is
riskier) and ``lpLockedPct`` (percent). A clean coin has no risks and a score of 1; the
``danger`` risks seen were "Top 10 holders high ownership", "Single holder ownership",
"High ownership", "Mint Authority still enabled" and "Large Amount of LP Unlocked".

The answer carried ``X-Rate-Limit-Limit: 15``; 30 requests 1.2 s apart all passed, so the
client asks at most one a second. A good report is held for ``cache_ttl`` (risks change
slowly), a failure for ``failure_ttl``. Nothing here raises into the cycle: an outage, a
429 or a changed format ends in a ``STALE`` report whose ``detail`` names the cause.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import ValidationError

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.market.models import Freshness, ProviderHealth, ProviderState, RugCheckReading
from src.observability.logging import get_logger
from src.providers.contracts import ProviderUnavailableError
from src.providers.resilience import CircuitBreaker, IntervalRateLimiter

RUGCHECK_URL = "https://api.rugcheck.xyz"
PROVIDER = "rugcheck"
DANGER = "danger"
REQUESTS_PER_SECOND = 1.0
RATE_LIMIT_PAUSE = timedelta(seconds=60)
_HUNDRED = Decimal(100)

Sleep = Callable[[float], Awaitable[None]]


class RugCheckProvider:
    """``report(mint) -> RugCheckReading`` and normalized ``health``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
        cache_ttl: timedelta = timedelta(minutes=30),
        failure_ttl: timedelta = timedelta(minutes=2),
        timeout: float = 10,
        failure_threshold: int = 3,
        recovery_timeout: float = 60,
    ) -> None:
        if cache_ttl <= timedelta(0) or failure_ttl <= timedelta(0):
            raise ValueError("cache_ttl and failure_ttl must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._failure_ttl = failure_ttl
        self._timeout = timeout
        self._limiter = IntervalRateLimiter(
            REQUESTS_PER_SECOND, monotonic=lambda: clock.now().timestamp(), sleep=sleep
        )
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            monotonic=lambda: clock.now().timestamp(),
        )
        self._paused_until: datetime | None = None
        self._reports: dict[str, RugCheckReading] = {}
        self._log = get_logger("providers.rugcheck")

    def __repr__(self) -> str:
        return f"RugCheckProvider(base_url={str(self._client.base_url)!r})"

    @property
    def health(self) -> ProviderHealth:
        paused = self._paused_until
        if paused is not None and self._clock.now() < paused:
            return ProviderHealth(ProviderState.OPEN, 1, "RATE_LIMITED", paused.timestamp())
        return self._breaker.health

    async def report(self, mint: PublicKey) -> RugCheckReading:
        held = self._held(mint.root)
        if held is not None:
            return held
        report = await self._read(mint)
        now = self._clock.now()
        self._reports = {
            key: item for key, item in self._reports.items() if self._alive(item, now)
        }
        self._reports[mint.root] = report
        return report

    def _held(self, key: str) -> RugCheckReading | None:
        held = self._reports.get(key)
        return held if held is not None and self._alive(held, self._clock.now()) else None

    def _alive(self, report: RugCheckReading, now: datetime) -> bool:
        ttl = self._cache_ttl if report.freshness is Freshness.FRESH else self._failure_ttl
        return now - report.source_at < ttl

    async def _read(self, mint: PublicKey) -> RugCheckReading:
        paused = self._paused_until
        if paused is not None and self._clock.now() < paused:
            return self._unavailable(mint, "RATE_LIMITED")
        try:
            self._breaker.before_request()
        except ProviderUnavailableError:
            return self._unavailable(mint, "CIRCUIT_OPEN")
        await self._limiter.acquire()
        try:
            response = await self._client.get(
                f"/v1/tokens/{mint.root}/report/summary", timeout=self._timeout
            )
        except httpx.TimeoutException:
            self._breaker.record_failure("timeout")
            return self._unavailable(mint, "API_TIMEOUT")
        except httpx.HTTPError:
            self._breaker.record_failure("network")
            return self._unavailable(mint, "API_UNAVAILABLE")
        if response.status_code == 429:
            self._paused_until = self._clock.now() + RATE_LIMIT_PAUSE
            return self._unavailable(mint, "RATE_LIMITED")
        if response.status_code in (400, 404):
            # RugCheck does not know the token; not an outage of the source.
            return self._unavailable(mint, "NOT_LISTED")
        if response.status_code >= 400:
            self._breaker.record_failure("server_error")
            return self._unavailable(mint, "API_UNAVAILABLE")
        try:
            body = response.json()
        except ValueError:
            body = None
        report = _report(body, mint, self._clock.now()) if isinstance(body, Mapping) else None
        if report is None:
            self._breaker.record_failure("malformed_payload")
            return self._unavailable(mint, "API_PAYLOAD_INVALID")
        self._breaker.record_success()
        return report

    def _unavailable(self, mint: PublicKey, detail: str) -> RugCheckReading:
        if detail != "NOT_LISTED":
            self._log.warning("rugcheck_unavailable", detail=detail)
        return RugCheckReading(
            mint=mint,
            provider=PROVIDER,
            source_at=self._clock.now(),
            freshness=Freshness.STALE,
            detail=detail,
        )


def _report(
    body: Mapping[str, Any], mint: PublicKey, source_at: datetime
) -> RugCheckReading | None:
    """The summary; ``None`` when its risk list is not one (a changed format)."""

    risks = body.get("risks")
    if risks is None:
        risks = []
    if not isinstance(risks, list):
        return None
    danger: list[str] = []
    warnings: list[str] = []
    for risk in risks:
        if not isinstance(risk, Mapping):
            return None
        name, level = risk.get("name"), risk.get("level")
        if not isinstance(name, str) or not name:
            return None
        (danger if level == DANGER else warnings).append(name)
    score = body.get("score_normalised")
    try:
        return RugCheckReading(
            mint=mint,
            provider=PROVIDER,
            source_at=source_at,
            freshness=Freshness.FRESH,
            score=score
            if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100
            else None,
            danger=tuple(danger),
            warnings=tuple(warnings),
            lp_locked_share=_share(body.get("lpLockedPct")),
        )
    except ValidationError:
        return None


def _share(percent: object) -> Decimal | None:
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    try:
        value = Decimal(repr(percent)) if isinstance(percent, float) else Decimal(percent)
    except InvalidOperation:
        return None
    if not value.is_finite() or not Decimal(0) <= value <= _HUNDRED:
        return None
    return value / _HUNDRED
