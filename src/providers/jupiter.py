"""``JupiterTokensProvider``: token readings from Jupiter Tokens API v2 (T22).

What is read - checked against live answers for the 62 tokens of the top 100 Meteora DLMM
pools by hourly volume on 2026-10-25: ``GET /tokens/v2/search?query=<mints>`` (up to 100
mints, comma-separated) answers a list of tokens, and of each, by ``id``:

* ``stats5m`` and ``stats1h``: ``buyVolume`` + ``sellVolume`` (USD), ``numBuys`` + ``numSells``
  and ``numTraders``;
* ``mcap``, ``usdPrice``, ``liquidity``, ``holderCount``, ``organicScore`` and
  ``organicScoreLabel`` (``low``, ``medium``, ``high``);
* ``audit``: ``mintAuthorityDisabled``, ``freezeAuthorityDisabled``, ``topHoldersPercentage``
  and ``devBalancePercentage`` (percent); ``firstPool.createdAt``.

Jupiter leaves zeros and false flags out of its answer. A window with no trades is
``{"volumeChange": -100}``, so inside a window that is there an absent volume or count is
zero, and a missing window is unknown. ``mintAuthorityDisabled`` is absent while the
authority is live, and ``mintAuthority`` then names it: an authority is REVOKED only on
``...Disabled: true``, PRESENT only when the address is named, UNKNOWN otherwise. An absent
percentage is unknown, never zero.

Limits as documented on 2026-10-25: 0.5 requests a second without a key, 1 with a free key
(header ``x-api-key``); the client keeps 80% of that. Nothing here raises into the cycle:
a refused key, a 429, an outage or a changed format ends in a ``STALE`` reading of each
token asked whose ``detail`` names the cause. The key exists only as ``SecretStr`` and in
the header of an outgoing request.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    AuthorityStatus,
    Freshness,
    JupiterTokenReading,
    OrganicScoreLabel,
    ProviderHealth,
    ProviderState,
)
from src.observability.logging import get_logger
from src.providers.contracts import ProviderUnavailableError
from src.providers.resilience import CircuitBreaker, IntervalRateLimiter

JUPITER_URL = "https://api.jup.ag"
SEARCH_PATH = "/tokens/v2/search"
API_KEY_HEADER = "x-api-key"
PROVIDER = "jupiter"
# The most mints one search takes.
MAX_BATCH = 100
KEYLESS_REQUESTS_PER_SECOND = 0.5
KEY_REQUESTS_PER_SECOND = 1.0
LIMIT_MARGIN = 0.8
# A 429 without a usable reset pauses every request this long; never longer than the ceiling.
RATE_LIMIT_FALLBACK = timedelta(seconds=60)
RATE_LIMIT_CEILING = timedelta(minutes=5)
_HUNDRED = Decimal(100)
_LABELS = {item.value: item for item in OrganicScoreLabel}

Sleep = Callable[[float], Awaitable[None]]


class JupiterTokensProvider:
    """``prefetch(mints)`` for a cycle, then ``reading(mint)`` without a request (T22).

    * one search per ``batch_size`` mints not held fresh, under one shared rate limiter;
    * a good reading is held for ``cache_ttl``; a token Jupiter does not list, or a batch
      that failed, is held as its failure reading for the same time, so the cycle shows why;
    * ``429``: no request until ``x-ratelimit-reset`` (or a minute), at most five minutes;
    * timeouts, outages, refusals and broken payloads count towards the circuit breaker.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock,
        api_key: SecretStr | None = None,
        sleep: Sleep = asyncio.sleep,
        cache_ttl: timedelta = timedelta(minutes=2),
        batch_size: int = MAX_BATCH,
        timeout: float = 10,
        failure_threshold: int = 3,
        recovery_timeout: float = 60,
    ) -> None:
        if cache_ttl <= timedelta(0):
            raise ValueError("cache_ttl must be positive")
        if not 1 <= batch_size <= MAX_BATCH:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client
        self._clock = clock
        self._api_key = api_key
        self._cache_ttl = cache_ttl
        self._batch_size = batch_size
        self._timeout = timeout
        rate = KEY_REQUESTS_PER_SECOND if api_key is not None else KEYLESS_REQUESTS_PER_SECOND
        self._limiter = IntervalRateLimiter(
            rate * LIMIT_MARGIN, monotonic=lambda: clock.now().timestamp(), sleep=sleep
        )
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            monotonic=lambda: clock.now().timestamp(),
        )
        self._paused_until: datetime | None = None
        self._readings: dict[str, JupiterTokenReading] = {}
        self._lock = asyncio.Lock()
        self._log = get_logger("providers.jupiter")

    def __repr__(self) -> str:
        keyed = self._api_key is not None
        return f"JupiterTokensProvider(base_url={str(self._client.base_url)!r}, key={keyed})"

    @property
    def health(self) -> ProviderHealth:
        paused = self._paused_until
        if paused is not None and self._clock.now() < paused:
            return ProviderHealth(ProviderState.OPEN, 1, "RATE_LIMITED", paused.timestamp())
        return self._breaker.health

    def reading(self, mint: PublicKey) -> JupiterTokenReading | None:
        """The reading of ``mint`` held at most ``cache_ttl``: good or its failure."""

        held = self._readings.get(mint.root)
        if held is None or self._clock.now() - held.source_at >= self._cache_ttl:
            return None
        return held

    async def prefetch(self, mints: Sequence[str]) -> None:
        """Ask Jupiter for every mint without a good reading held; one cycle at a time."""

        async with self._lock:
            now = self._clock.now()
            self._readings = {
                key: item
                for key, item in self._readings.items()
                if now - item.source_at < self._cache_ttl
            }
            wanted = [
                mint
                for mint in dict.fromkeys(mints)
                if (held := self._readings.get(mint)) is None
                or held.freshness is not Freshness.FRESH
            ]
            for start in range(0, len(wanted), self._batch_size):
                await self._read_batch(wanted[start : start + self._batch_size])

    async def _read_batch(self, batch: list[str]) -> None:
        paused = self._paused_until
        if paused is not None and self._clock.now() < paused:
            self._fail(batch, "RATE_LIMITED")
            return
        try:
            self._breaker.before_request()
        except ProviderUnavailableError:
            self._fail(batch, "CIRCUIT_OPEN")
            return
        await self._limiter.acquire()
        headers = (
            {}
            if self._api_key is None
            else {API_KEY_HEADER: self._api_key.get_secret_value()}
        )
        try:
            response = await self._client.get(
                SEARCH_PATH,
                params={"query": ",".join(batch)},
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            self._breaker.record_failure("timeout")
            self._fail(batch, "API_TIMEOUT")
            return
        except httpx.HTTPError:
            self._breaker.record_failure("network")
            self._fail(batch, "API_UNAVAILABLE")
            return
        if response.status_code == 429:
            self._paused_until = self._clock.now() + _pause(response, self._clock.now())
            self._fail(batch, "RATE_LIMITED")
            return
        if response.status_code in (401, 403):
            self._breaker.record_failure("client_error")
            self._fail(batch, f"HTTP_{response.status_code}")
            return
        if response.status_code >= 400:
            self._breaker.record_failure("server_error")
            self._fail(batch, "API_UNAVAILABLE")
            return
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, list):
            self._breaker.record_failure("malformed_payload")
            self._fail(batch, "API_PAYLOAD_INVALID")
            return
        self._breaker.record_success()
        source_at = self._clock.now()
        asked = set(batch)
        for item in body:
            if not isinstance(item, Mapping):
                continue
            mint = item.get("id")
            if not isinstance(mint, str) or mint not in asked:
                continue
            asked.discard(mint)
            self._readings[mint] = _reading(item, mint, source_at)
        # Not an outage: Jupiter does not know these tokens (a fresh launch, a dead coin).
        self._fail(sorted(asked), "NOT_LISTED", log=False)

    def _fail(self, mints: Sequence[str], detail: str, *, log: bool = True) -> None:
        if not mints:
            return
        now = self._clock.now()
        if log:
            self._log.warning("jupiter_tokens_unavailable", detail=detail, tokens=len(mints))
        for mint in mints:
            self._readings[mint] = JupiterTokenReading(
                mint=PublicKey(mint),
                provider=PROVIDER,
                source_at=now,
                freshness=Freshness.STALE,
                detail=detail,
            )


def _pause(response: httpx.Response, now: datetime) -> timedelta:
    """Until ``x-ratelimit-reset`` (Unix seconds) and a second more, within the bounds."""

    raw = response.headers.get("x-ratelimit-reset", "")
    try:
        reset = datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return RATE_LIMIT_FALLBACK
    wait = reset - now + timedelta(seconds=1)
    if wait <= timedelta(0):
        return RATE_LIMIT_FALLBACK
    return min(wait, RATE_LIMIT_CEILING)


def _reading(item: Mapping[str, Any], mint: str, source_at: datetime) -> JupiterTokenReading:
    """One listed token; a field in another shape is left unknown, never guessed."""

    short = _mapping(item.get("stats5m"))
    hour = _mapping(item.get("stats1h"))
    audit = _mapping(item.get("audit")) or {}
    label = item.get("organicScoreLabel")
    values: dict[str, Any] = {
        "mint": PublicKey(mint),
        "provider": PROVIDER,
        "source_at": source_at,
        "freshness": Freshness.FRESH,
        "volume_5m_usd": _window_sum(short, "buyVolume", "sellVolume"),
        "volume_1h_usd": _window_sum(hour, "buyVolume", "sellVolume"),
        "trades_5m": _window_count(short, "numBuys", "numSells"),
        "traders_5m": _window_count(short, "numTraders"),
        "price_usd": _amount(item.get("usdPrice")),
        "market_cap_usd": _amount(item.get("mcap")),
        "liquidity_usd": _amount(item.get("liquidity")),
        "holder_count": _count(item.get("holderCount")),
        "organic_score": _bounded(_amount(item.get("organicScore")), _HUNDRED),
        "organic_score_label": _LABELS.get(label) if isinstance(label, str) else None,
        "mint_authority": _authority(
            audit.get("mintAuthorityDisabled"), item.get("mintAuthority")
        ),
        "freeze_authority": _authority(
            audit.get("freezeAuthorityDisabled"), item.get("freezeAuthority")
        ),
        "top_holders_share": _share(audit.get("topHoldersPercentage")),
        "developer_share": _share(audit.get("devBalancePercentage")),
        "first_pool_at": _timestamp(_mapping(item.get("firstPool")) or {}, "createdAt"),
    }
    try:
        return JupiterTokenReading.model_validate(values)
    except ValidationError:
        return JupiterTokenReading(
            mint=PublicKey(mint),
            provider=PROVIDER,
            source_at=source_at,
            freshness=Freshness.STALE,
            detail="API_PAYLOAD_INVALID",
        )


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _amount(value: object) -> Decimal | None:
    """A non-negative JSON number as the decimal it was written as."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _bounded(value: Decimal | None, maximum: Decimal) -> Decimal | None:
    return value if value is not None and value <= maximum else None


def _window_sum(window: Mapping[str, Any] | None, *names: str) -> Decimal | None:
    """The window's figures summed; absent = zero (Jupiter omits zeros), malformed = unknown."""

    if window is None:
        return None
    total = Decimal(0)
    for name in names:
        if name not in window:
            continue
        value = _amount(window[name])
        if value is None:
            return None
        total += value
    return total


def _window_count(window: Mapping[str, Any] | None, *names: str) -> int | None:
    if window is None:
        return None
    total = 0
    for name in names:
        if name not in window:
            continue
        value = _count(window[name])
        if value is None:
            return None
        total += value
    return total


def _share(percent: object) -> Decimal | None:
    value = _amount(percent)
    return None if value is None or value > _HUNDRED else value / _HUNDRED


def _authority(disabled: object, address: object) -> AuthorityStatus:
    if disabled is True:
        return AuthorityStatus.REVOKED
    if isinstance(address, str) and address:
        return AuthorityStatus.PRESENT
    return AuthorityStatus.UNKNOWN


def _timestamp(item: Mapping[str, Any], name: str) -> datetime | None:
    value = item.get(name)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
