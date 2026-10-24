"""``GmgnOpenApiProvider``: GMGN metrics of a candidate from the official GMGN OpenAPI (G20).

Where the address and the key header come from - the official CLI ``gmgn-cli`` of
https://github.com/GMGNAI/gmgn-skills (checked 2026-10-24):

* ``src/config.ts``: ``host = "https://openapi.gmgn.ai"``;
* ``src/client/OpenApiClient.ts``, ``authExistRequest``: header ``X-APIKEY`` with the key and
  the query ``timestamp`` (Unix seconds) and ``client_id`` (a fresh UUID). Token routes
  need no signature (``X-Signature`` is only for swap and order routes);
* ``docs/cli-usage.md`` ("Rate Limit Handling", "Error Codes") and
  ``skills/gmgn-token/SKILL.md``: a leaky bucket per plan (Free 5/5, Plus 20/20, Pro 50/50,
  rate/capacity), route weights, ``429`` with ``X-RateLimit-Reset`` and ``reset_at``
  (requests during a ban extend it), ``403 PLAN_UPGRADE_REQUIRED``, IPv4 only.

What is read (checked against a live answer for WIF on 2026-10-24; the field names are the
ones of the token page's ``token_stat`` that the browser service reads):

* ``GET /v1/token/info`` (weight 1): ``stat.top_rat_trader_percentage`` (insiders),
  ``stat.top_bundler_trader_percentage`` (bundling), ``stat.top_entrapment_trader_percentage``
  (phishing), ``stat.top_10_holder_rate`` (top-10), ``stat.creator_hold_rate`` and
  ``stat.dev_team_hold_rate`` (developer), and ``total_fee`` - the token's total fees in SOL,
  the page's "Total Fees" (it matched the browser service's figure for WIF);
* ``GET /v1/token/security`` (weight 1), asked only after a good info answer: it fills what
  info lacks (``top_10_holder_rate``, ``rat_trader_amount_rate``,
  ``bundler_trader_amount_rate``) and its ``dev_team_hold_rate`` / ``creator_balance_rate``
  join the developer share, which is the largest holding reported - never under-reported.

Nothing here raises into the candidate pipeline: a refused key, a ban, an outage or a
changed format ends in a ``STALE`` reading whose ``detail`` names the cause, or in a fresh
reading without the metric that changed. The key exists only as ``SecretStr`` and in the
header of an outgoing request: never in a URL, a log record, a reading or a reason.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.gmgn.client import AMOUNT_SIGNALS, SHARE_SIGNALS
from src.market.models import (
    DeveloperStatus,
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    ProviderState,
    RiskSignal,
)
from src.observability.logging import get_logger
from src.providers.contracts import ProviderUnavailableError
from src.providers.resilience import CircuitBreaker

OPENAPI_URL = "https://openapi.gmgn.ai"
API_KEY_HEADER = "X-APIKEY"
PROVIDER = "gmgn-openapi"
TOKEN_INFO = "/v1/token/info"  # noqa: S105 - a route, not a secret
TOKEN_SECURITY = "/v1/token/security"  # noqa: S105 - a route, not a secret
ROUTE_WEIGHTS = {TOKEN_INFO: 1, TOKEN_SECURITY: 1}

API_KEY_ENV = "GMGN_API_KEY"
API_URL_ENV = "GMGN_API_URL"
API_PLAN_ENV = "GMGN_API_PLAN"
API_CACHE_ENV = "GMGN_API_CACHE_SECONDS"

# The client keeps this share of the plan's bucket, so it stays below the server's limit.
LIMIT_MARGIN = 0.8
# Without any reset time in a 429 the ban is assumed to last this long.
RATE_LIMIT_FALLBACK = timedelta(seconds=60)
# Requests resume a little after the announced reset, never exactly on it.
RESET_BUFFER = timedelta(seconds=1)
PLAN_UPGRADE_REQUIRED = "PLAN_UPGRADE_REQUIRED"

# An API error string is passed on only when it looks like an error code and carries no
# piece of the key (a server may echo the key it refused, in any letter case).
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_KEY_FRAGMENT = 8

Sleep = Callable[[float], Awaitable[None]]


class GmgnPlan(StrEnum):
    """The user's GMGN OpenAPI plan; its bucket is ``(rate per second, capacity)``."""

    FREE = "free"
    PLUS = "plus"
    PRO = "pro"

    @property
    def bucket(self) -> tuple[float, float]:
        return _PLAN_BUCKETS[self]


_PLAN_BUCKETS = {GmgnPlan.FREE: (5.0, 5.0), GmgnPlan.PLUS: (20.0, 20.0), GmgnPlan.PRO: (50.0, 50.0)}


@dataclass(frozen=True, slots=True)
class OpenApiSettings:
    """``GMGN_API_KEY`` (or ``GMGN_API_KEY_FILE``), ``GMGN_API_URL``, ``GMGN_API_PLAN``,
    ``GMGN_API_CACHE_SECONDS``; ``None`` without a key."""

    api_key: SecretStr
    base_url: str
    plan: GmgnPlan
    cache_ttl: timedelta

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "OpenApiSettings | None":
        key = _secret(environ, API_KEY_ENV)
        if key is None:
            return None
        raw_plan = (environ.get(API_PLAN_ENV) or GmgnPlan.FREE.value).strip().lower()
        try:
            plan = GmgnPlan(raw_plan)
        except ValueError:
            names = ", ".join(item.value for item in GmgnPlan)
            raise ValueError(f"{API_PLAN_ENV} must be one of: {names}") from None
        raw_cache = (environ.get(API_CACHE_ENV) or "").strip()
        cache_seconds = int(raw_cache) if raw_cache else 300
        if cache_seconds <= 0:
            raise ValueError(f"{API_CACHE_ENV} must be a positive number of seconds")
        return cls(
            api_key=key,
            base_url=(environ.get(API_URL_ENV) or "").strip() or OPENAPI_URL,
            plan=plan,
            cache_ttl=timedelta(seconds=cache_seconds),
        )


def _secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    """``NAME`` or ``NAME_FILE``, never both - as every other secret of the project."""

    direct = (environ.get(name) or "").strip()
    file_name = (environ.get(f"{name}_FILE") or "").strip()
    if direct and file_name:
        raise ValueError(f"set either {name} or {name}_FILE, not both")
    if direct:
        return SecretStr(direct)
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"{name}_FILE contains an empty secret")
        return SecretStr(value)
    return None


def ipv4_transport() -> httpx.AsyncHTTPTransport:
    """Connections from an IPv4 source address only: over IPv6 GMGN answers 401/403."""

    return httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=0)  # noqa: S104


def openapi_client(
    settings: OpenApiSettings,
    *,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The HTTP client of the provider; the key is not a default header of it."""

    return httpx.AsyncClient(
        base_url=settings.base_url,
        timeout=timeout,
        transport=transport or ipv4_transport(),
        headers={"Accept": "application/json", "User-Agent": "meteora-volume-bot"},
    )


class LeakyBucket:
    """Client-side bucket: ``rate`` units per second, at most ``capacity`` at once.

    Waiting callers queue behind one lock, so the order of sends is the order of calls.
    """

    def __init__(self, *, rate: float, capacity: float, clock: Clock, sleep: Sleep) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("the bucket needs a positive rate and room for one request")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._level = capacity
        self._updated = clock.now()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int, *, max_wait: float) -> bool:
        """Take ``weight`` units, waiting for them; ``False`` if that takes over ``max_wait``."""

        async with self._lock:
            self._refill()
            missing = weight - self._level
            if missing > 0:
                wait = missing / self._rate
                if wait > max_wait:
                    return False
                await self._sleep(wait)
                self._refill()
            self._level -= weight
            return True

    def _refill(self) -> None:
        now = self._clock.now()
        elapsed = max((now - self._updated).total_seconds(), 0.0)
        self._level = min(self._capacity, self._level + elapsed * self._rate)
        self._updated = now


@dataclass(frozen=True, slots=True)
class _Refused:
    reason: str


class GmgnOpenApiProvider:
    """``get_token_signals(mint) -> EnhancedTokenSignals`` and normalized ``health``.

    * one shared client-side bucket below the plan (``LIMIT_MARGIN``), route weights
      included; a wait longer than ``max_wait`` answers ``RATE_LIMIT_LOCAL_QUEUE`` instead;
    * ``429``: no request at all until the reset (the later of ``X-RateLimit-Reset`` and
      ``reset_at``), at most ``cooldown_ceiling``;
    * ``401``/``403`` (key, IP, timestamp, IPv6) and ``PLAN_UPGRADE_REQUIRED``: unavailable
      with the reason and no request for ``cooldown_ceiling`` - retries cannot fix them;
    * timeouts, outages and broken payloads count towards the circuit breaker;
    * a good reading is cached per token for ``cache_ttl`` (bounded by ``stale_after``), and
      concurrent calls for one token share a single fetch.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
        chain: str = "sol",
        plan: GmgnPlan = GmgnPlan.FREE,
        stale_after: timedelta = timedelta(minutes=15),
        cache_ttl: timedelta = timedelta(minutes=5),
        timeout: float = 10,
        max_wait: float = 30,
        failure_threshold: int = 3,
        recovery_timeout: float = 60,
        cooldown_ceiling: timedelta = timedelta(minutes=15),
    ) -> None:
        if stale_after <= timedelta(0) or cache_ttl <= timedelta(0):
            raise ValueError("stale_after and cache_ttl must be positive")
        self._client = client
        self._api_key = api_key
        self._clock = clock
        self._chain = chain
        self._plan = plan
        self._stale_after = stale_after
        self._cache_ttl = min(cache_ttl, stale_after)
        self._timeout = timeout
        self._max_wait = max_wait
        self._cooldown_ceiling = cooldown_ceiling
        rate, capacity = plan.bucket
        self._bucket = LeakyBucket(
            rate=rate * LIMIT_MARGIN,
            capacity=max(1.0, float(int(capacity * LIMIT_MARGIN))),
            clock=clock,
            sleep=sleep,
        )
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            monotonic=lambda: clock.now().timestamp(),
        )
        self._blocked_until: datetime | None = None
        self._block_reason: str | None = None
        self._route_blocks: dict[str, tuple[datetime, str]] = {}
        self._cache: dict[str, EnhancedTokenSignals] = {}
        self._inflight: dict[str, asyncio.Task[EnhancedTokenSignals]] = {}
        self._unavailable_in_row = 0
        self._last_detail: str | None = None
        self._log = get_logger("gmgn.openapi")

    def __repr__(self) -> str:
        return f"GmgnOpenApiProvider(base_url={str(self._client.base_url)!r}, plan={self._plan})"

    @property
    def health(self) -> ProviderHealth:
        now = self._clock.now()
        if self._blocked_until is not None and now < self._blocked_until:
            return ProviderHealth(
                ProviderState.OPEN,
                self._unavailable_in_row,
                self._block_reason,
                self._blocked_until.timestamp(),
            )
        transport = self._breaker.health
        if transport.state is not ProviderState.HEALTHY or not self._unavailable_in_row:
            return transport
        return ProviderHealth(
            ProviderState.DEGRADED, self._unavailable_in_row, self._last_detail, None
        )

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
        key = mint.root
        cached = self._cache.get(key)
        if cached is not None and self._clock.now() - cached.source_at < self._cache_ttl:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._fetch(mint))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        return await asyncio.shield(task)

    def _forget(self, key: str, task: "asyncio.Task[EnhancedTokenSignals]") -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]

    async def _fetch(self, mint: PublicKey) -> EnhancedTokenSignals:
        signals = await self._read(mint)
        if signals.detail is None:
            self._unavailable_in_row = 0
            self._last_detail = None
            self._remember(mint.root, signals)
        else:
            self._unavailable_in_row += 1
            self._last_detail = signals.detail
        return signals

    def _remember(self, key: str, signals: EnhancedTokenSignals) -> None:
        now = self._clock.now()
        self._cache = {
            name: item
            for name, item in self._cache.items()
            if now - item.source_at < self._cache_ttl
        }
        self._cache[key] = signals

    async def _read(self, mint: PublicKey) -> EnhancedTokenSignals:
        info = await self._call(TOKEN_INFO, mint)
        if isinstance(info, _Refused):
            return self._unavailable(info.reason)
        if info.get("address") != mint.root:
            return self._unavailable("API_MINT_MISMATCH")
        source_at = self._clock.now()
        security = await self._call(TOKEN_SECURITY, mint)
        extra = (
            security if isinstance(security, dict) and security.get("address") == mint.root else {}
        )
        return self._signals(info, extra, source_at)

    def _signals(
        self, info: Mapping[str, Any], security: Mapping[str, Any], source_at: datetime
    ) -> EnhancedTokenSignals:
        stat = info.get("stat")
        stat = stat if isinstance(stat, Mapping) else {}
        dev = info.get("dev")
        dev = dev if isinstance(dev, Mapping) else {}
        developer = [
            value
            for value in (
                _share(stat.get("creator_hold_rate")),
                _share(stat.get("dev_team_hold_rate")),
                _share(security.get("dev_team_hold_rate")),
                _share(security.get("creator_balance_rate")),
            )
            if value is not None
        ]
        values: dict[str, Decimal | None] = {
            "top10_holder_share": _first(
                _share(stat.get("top_10_holder_rate")),
                _share(security.get("top_10_holder_rate")),
                _share(dev.get("top_10_holder_rate")),
            ),
            "insiders_share": _first(
                _share(stat.get("top_rat_trader_percentage")),
                _share(security.get("rat_trader_amount_rate")),
            ),
            "bundling_share": _first(
                _share(stat.get("top_bundler_trader_percentage")),
                _share(security.get("bundler_trader_amount_rate")),
            ),
            "phishing_share": _share(stat.get("top_entrapment_trader_percentage")),
            "developer_share": max(developer) if developer else None,
            "total_fees_sol": _amount(info.get("total_fee")),
        }
        missing = tuple(name for name in (*SHARE_SIGNALS, *AMOUNT_SIGNALS) if values[name] is None)
        market = _market(info, stat, dev, security)
        return EnhancedTokenSignals(
            top10_holder_share=values["top10_holder_share"],
            insiders_share=values["insiders_share"],
            bundling_share=values["bundling_share"],
            phishing_share=values["phishing_share"],
            developer_share=values["developer_share"],
            total_fees_sol=values["total_fees_sol"],
            missing_signals=missing,
            provider=PROVIDER,
            source_at=source_at,
            freshness=Freshness.FRESH,
            **market,
        )

    async def _call(self, route: str, mint: PublicKey) -> dict[str, Any] | _Refused:
        """One GET of ``route`` within every limit; the ``data`` object or the reason."""

        blocked = self._blocked(route)
        if blocked is not None:
            return _Refused(blocked)
        if not await self._bucket.acquire(ROUTE_WEIGHTS[route], max_wait=self._max_wait):
            return _Refused("RATE_LIMIT_LOCAL_QUEUE")
        # A concurrent answer may have started a ban while this call waited for the bucket.
        blocked = self._blocked(route)
        if blocked is not None:
            return _Refused(blocked)
        try:
            self._breaker.before_request()
        except ProviderUnavailableError:
            return _Refused("CIRCUIT_OPEN")
        try:
            response = await self._client.get(
                route,
                params={
                    "chain": self._chain,
                    "address": mint.root,
                    "timestamp": str(int(self._clock.now().timestamp())),
                    "client_id": str(uuid4()),
                },
                headers={API_KEY_HEADER: self._api_key.get_secret_value()},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            self._breaker.record_failure("API_TIMEOUT")
            return _Refused("API_TIMEOUT")
        except httpx.HTTPError:
            self._breaker.record_failure("API_UNAVAILABLE")
            return _Refused("API_UNAVAILABLE")
        return self._answer(route, response)

    def _answer(self, route: str, response: httpx.Response) -> dict[str, Any] | _Refused:
        body = _json(response.content)
        code = _error_code(body, self._api_key)
        status = response.status_code
        if status == 429:
            reason = code or "RATE_LIMITED"
            self._block_all(self._reset_at(response, body), reason)
            return _Refused(reason)
        if status in (401, 403):
            reason = code or f"HTTP_{status}"
            until = self._clock.now() + self._cooldown_ceiling
            if reason == PLAN_UPGRADE_REQUIRED:
                self._route_blocks[route] = (until, reason)
                self._log.warning(
                    "gmgn_openapi_blocked", route=route, reason=reason, until=until.isoformat()
                )
            else:
                self._block_all(until, reason)
            return _Refused(reason)
        if status >= 500:
            reason = code or "API_UNAVAILABLE"
            self._breaker.record_failure(reason)
            return _Refused(reason)
        if status != 200:
            return _Refused(code or f"HTTP_{status}")
        if not isinstance(body, dict) or "code" not in body:
            self._breaker.record_failure("API_PAYLOAD_INVALID")
            return _Refused("API_PAYLOAD_INVALID")
        if body["code"] != 0 or isinstance(body["code"], bool):
            return _Refused(code or "API_ERROR")
        data = body.get("data")
        if not isinstance(data, dict):
            self._breaker.record_failure("API_PAYLOAD_INVALID")
            return _Refused("API_PAYLOAD_INVALID")
        self._breaker.record_success()
        return data

    def _reset_at(self, response: httpx.Response, body: object) -> datetime:
        now = self._clock.now()
        candidates = [
            moment
            for moment in (
                _unix(response.headers.get("X-RateLimit-Reset")),
                _unix(body.get("reset_at")) if isinstance(body, dict) else None,
            )
            if moment is not None
        ]
        until = max(candidates) + RESET_BUFFER if candidates else now + RATE_LIMIT_FALLBACK
        return min(max(until, now + RESET_BUFFER), now + self._cooldown_ceiling)

    def _block_all(self, until: datetime, reason: str) -> None:
        self._blocked_until = until
        self._block_reason = reason
        self._log.warning("gmgn_openapi_blocked", reason=reason, until=until.isoformat())

    def _blocked(self, route: str) -> str | None:
        now = self._clock.now()
        if self._blocked_until is not None and now < self._blocked_until:
            return self._block_reason
        route_block = self._route_blocks.get(route)
        if route_block is not None and now < route_block[0]:
            return route_block[1]
        return None

    def _unavailable(self, detail: str) -> EnhancedTokenSignals:
        return EnhancedTokenSignals(
            missing_signals=(*SHARE_SIGNALS, *AMOUNT_SIGNALS),
            provider=PROVIDER,
            source_at=self._clock.now(),
            freshness=Freshness.STALE,
            detail=detail,
        )


def _json(content: bytes) -> Any:
    """JSON with exact decimals (``parse_float=Decimal``); ``None`` for anything else."""

    try:
        return json.loads(content, parse_float=Decimal)
    except (ValueError, RecursionError):
        return None


def _error_code(body: object, api_key: SecretStr) -> str | None:
    """The server's ``error`` when it is a code; never one that echoes the key (A4)."""

    if not isinstance(body, dict):
        return None
    value = body.get("error")
    if (
        isinstance(value, str)
        and _ERROR_CODE.fullmatch(value)
        and not _echoes_key(value, api_key.get_secret_value())
    ):
        return value
    return None


def _echoes_key(code: str, key: str) -> bool:
    """``code`` is the key, contains it, or contains any ``_KEY_FRAGMENT`` characters of it."""

    text, secret = code.casefold(), key.casefold()
    if not secret:
        return False
    if secret in text:
        return True
    return any(
        secret[start : start + _KEY_FRAGMENT] in text
        for start in range(len(secret) - _KEY_FRAGMENT + 1)
    )


def _unix(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return number if number.is_finite() else None


def _share(value: object) -> Decimal | None:
    number = _number(value)
    if number is None or not Decimal(0) <= number <= Decimal(1):
        return None
    return number


def _amount(value: object) -> Decimal | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return number


def _first(*values: Decimal | None) -> Decimal | None:
    return next((value for value in values if value is not None), None)


# ``creator_token_status``: token info answers ``creator_hold``/``creator_close`` live
# (2026-10-24) while gmgn-token SKILL.md documents ``hold``/``sell`` for info and
# ``creator_hold``/``creator_close`` for security; both vocabularies are understood.
_DEVELOPER_STATUSES = {
    "creator_hold": DeveloperStatus.HOLDING,
    "hold": DeveloperStatus.HOLDING,
    "creator_close": DeveloperStatus.SOLD,
    "creator_sell": DeveloperStatus.SOLD,
    "sell": DeveloperStatus.SOLD,
    "close": DeveloperStatus.SOLD,
}


def _market(
    info: Mapping[str, Any],
    stat: Mapping[str, Any],
    dev: Mapping[str, Any],
    security: Mapping[str, Any],
) -> dict[str, Any]:
    """Token-level market figures of token info (T18) - the same request, no extra weight.

    Source, checked against gmgn-token SKILL.md and the live answers of 2026-10-24 (WIF and
    a trending pump.fun token): ``price.volume_5m``/``price.volume_1h`` are USD over every
    pool of the token (the "1h" volume matched the trending rank's own hourly volume), so
    no kline request is needed; ``price.price`` is USD and the market cap, which info does
    not serve, is ``price.price x circulating_supply`` as the SKILL.md says; ``ath_price``
    is the token's own ATH in USD (``dev.ath_token_info`` is the creator's best token, not
    this one). Snipers: ``wallet_tags_stat.sniper_wallets`` (security's documented
    ``sniper_count`` when info has none) and ``stat.top70_sniper_hold_rate``. The creator's
    status: ``dev.creator_token_status``, else security's. Wash trading:
    ``is_wash_trading`` of security (documented) or info; absent - unknown, never "clean".
    A figure that is missing or malformed stays ``None``; the reading itself is kept.
    """

    price_section = info.get("price")
    prices: Mapping[str, Any] = price_section if isinstance(price_section, Mapping) else {}
    tags = info.get("wallet_tags_stat")
    wallets: Mapping[str, Any] = tags if isinstance(tags, Mapping) else {}
    price = _positive(prices.get("price"))
    ath = _positive(info.get("ath_price"))
    supply = _positive(info.get("circulating_supply"))
    snipers = _count(wallets.get("sniper_wallets"))
    wash = _verdict(security.get("is_wash_trading"))
    return {
        "volume_5m_usd": _amount(prices.get("volume_5m")),
        "volume_1h_usd": _amount(prices.get("volume_1h")),
        "price_usd": price,
        "market_cap_usd": None if price is None or supply is None else price * supply,
        "ath_price_usd": ath,
        "ath_market_cap_usd": None if ath is None or supply is None else ath * supply,
        "sniper_count": snipers if snipers is not None else _count(security.get("sniper_count")),
        "sniper_share": _share(stat.get("top70_sniper_hold_rate")),
        "developer_status": _developer_status(dev.get("creator_token_status"))
        or _developer_status(security.get("creator_token_status")),
        "created_at": _unix(info.get("creation_timestamp")),
        "wash_trading": wash
        if wash is not RiskSignal.UNKNOWN
        else _verdict(info.get("is_wash_trading")),
    }


def _positive(value: object) -> Decimal | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _developer_status(value: object) -> DeveloperStatus | None:
    if not isinstance(value, str):
        return None
    return _DEVELOPER_STATUSES.get(value.strip().lower())


def _verdict(value: object) -> RiskSignal:
    if value is True:
        return RiskSignal.UNSAFE
    if value is False:
        return RiskSignal.SAFE
    return RiskSignal.UNKNOWN
