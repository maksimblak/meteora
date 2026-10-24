"""``GmgnProvider``: the enhanced-signal provider of the worker backed by the ``gmgn`` service.

Two services answer ``GET /v1/tokens/{chain}/{mint}/signals`` (``services/gmgn``):

* the browser bridge (G25) - a visible Chromium window on the operator's PC, reached at
  ``GMGN_URL`` (e.g. ``http://host.docker.internal:8765``) with the shared token
  ``GMGN_BRIDGE_TOKEN``; it answers ``gmgn-signals.v2``: the shares and total fees of v1 plus
  the token's volume 5m/1h, price, market cap, ATH, snipers, the developer's status, the
  creation time and the wash-trading verdict - the fields the OpenAPI provider fills;
* the headless page service (G18), compose profile ``gmgn``, answering ``gmgn-signals.v1``.

Nothing here raises into the candidate pipeline: an unreachable service, a refused token,
an open breaker, a page behind a challenge, an unknown schema or a malformed field all end
in a reading that is ``STALE`` with ``detail`` naming the cause, or in a fresh reading
without the metric that changed its format. Fresh means the data are at most
``stale_after`` old. A fresh reading is kept for ``cache_ttl`` and ranks tokens (F5,
``cached_signals``) exactly as the OpenAPI provider's do.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    DeveloperStatus,
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    ProviderState,
    RiskSignal,
)
from src.providers.contracts import ProviderUnavailableError
from src.providers.resilience import CircuitBreaker

SCHEMA_VERSION = "gmgn-signals.v1"
SCHEMA_VERSION_V2 = "gmgn-signals.v2"
SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, SCHEMA_VERSION_V2})
PROVIDER = "gmgn"
BRIDGE_TOKEN_ENV = "GMGN_BRIDGE_TOKEN"  # noqa: S105 - a variable name
# Fractions in [0, 1] and the non-negative SOL amount, as the service names them.
SHARE_SIGNALS = (
    "top10_holder_share",
    "insiders_share",
    "bundling_share",
    "phishing_share",
    "developer_share",
)
AMOUNT_SIGNALS = ("total_fees_sol",)
# v2: the token's market figures in USD, as the OpenAPI provider names them (T18).
USD_SIGNALS = (
    "volume_5m_usd",
    "volume_1h_usd",
    "price_usd",
    "market_cap_usd",
    "ath_price_usd",
    "ath_market_cap_usd",
)
_DEVELOPER_STATUSES = {item.value: item for item in DeveloperStatus}


def bridge_token(environ: Mapping[str, str]) -> SecretStr | None:
    """``GMGN_BRIDGE_TOKEN`` or ``GMGN_BRIDGE_TOKEN_FILE`` (never both), as every secret."""

    direct = (environ.get(BRIDGE_TOKEN_ENV) or "").strip()
    file_name = (environ.get(f"{BRIDGE_TOKEN_ENV}_FILE") or "").strip()
    if direct and file_name:
        raise ValueError(f"set either {BRIDGE_TOKEN_ENV} or {BRIDGE_TOKEN_ENV}_FILE, not both")
    if direct:
        return SecretStr(direct)
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"{BRIDGE_TOKEN_ENV}_FILE contains an empty secret")
        return SecretStr(value)
    return None


class GmgnProvider:
    """``get_token_signals(mint) -> EnhancedTokenSignals``, ``cached_signals`` and ``health``.

    One attempt per call: the service caches, backs off and breaks its own circuit, so the
    worker never repeats a load. ``failure_threshold`` failed calls in a row open the
    worker-side breaker for ``recovery_timeout`` seconds; meanwhile no request is sent.
    ``token`` (the bridge's shared token) goes only into the ``Authorization`` header.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock,
        chain: str = "sol",
        stale_after: timedelta = timedelta(minutes=15),
        cache_ttl: timedelta = timedelta(minutes=5),
        timeout: float = 20,
        failure_threshold: int = 3,
        recovery_timeout: float = 60,
        token: SecretStr | None = None,
    ) -> None:
        if stale_after <= timedelta(0) or cache_ttl <= timedelta(0):
            raise ValueError("stale_after and cache_ttl must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client
        self._clock = clock
        self._chain = chain
        self._stale_after = stale_after
        self._cache_ttl = min(cache_ttl, stale_after)
        self._timeout = timeout
        self._token = token
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold, recovery_timeout=recovery_timeout
        )
        self._cache: dict[str, EnhancedTokenSignals] = {}
        self._unavailable_in_row = 0
        self._last_detail: str | None = None

    def __repr__(self) -> str:
        return f"GmgnProvider(base_url={str(self._client.base_url)!r}, chain={self._chain!r})"

    @property
    def health(self) -> ProviderHealth:
        transport = self._breaker.health
        if transport.state is not ProviderState.HEALTHY or not self._unavailable_in_row:
            return transport
        # The service answered, but without data (challenge, captcha, its own breaker).
        return ProviderHealth(
            ProviderState.DEGRADED, self._unavailable_in_row, self._last_detail, None
        )

    def cached_signals(self, mint: PublicKey) -> EnhancedTokenSignals | None:
        """The fresh reading of ``mint`` still held, or ``None``; never a request (F5)."""

        cached = self._cache.get(mint.root)
        if cached is not None and self._clock.now() - cached.source_at < self._cache_ttl:
            return cached
        return None

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
        cached = self.cached_signals(mint)
        if cached is not None:
            return cached
        signals = await self._read(mint)
        if signals.detail is None:
            self._unavailable_in_row = 0
            self._last_detail = None
            if signals.freshness is Freshness.FRESH:
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
        try:
            self._breaker.before_request()
        except ProviderUnavailableError:
            return self._unavailable("CIRCUIT_OPEN")
        headers = (
            {}
            if self._token is None
            else {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        )
        try:
            response = await self._client.get(
                f"/v1/tokens/{self._chain}/{mint.root}/signals",
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            self._breaker.record_failure("timeout")
            return self._unavailable("SERVICE_TIMEOUT")
        except httpx.HTTPError:
            self._breaker.record_failure("network")
            return self._unavailable("SERVICE_UNAVAILABLE")
        if response.status_code in (401, 403):
            self._breaker.record_failure("client_error")
            return self._unavailable("SERVICE_UNAUTHORIZED")
        if response.status_code >= 400:
            self._breaker.record_failure("server_error")
            return self._unavailable("SERVICE_UNAVAILABLE")
        try:
            body = response.json()
        except ValueError:
            self._breaker.record_failure("malformed_payload")
            return self._unavailable("SERVICE_PAYLOAD_INVALID")
        self._breaker.record_success()
        return self._parse(body, mint)

    def _parse(self, body: Any, mint: PublicKey) -> EnhancedTokenSignals:
        if not isinstance(body, dict) or body.get("schema_version") not in SCHEMA_VERSIONS:
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
        market = _market(raw_signals) if body["schema_version"] == SCHEMA_VERSION_V2 else {}
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
            **market,
        )

    def _unavailable(self, detail: str) -> EnhancedTokenSignals:
        return EnhancedTokenSignals(
            missing_signals=(*SHARE_SIGNALS, *AMOUNT_SIGNALS),
            provider=PROVIDER,
            source_at=self._clock.now(),
            freshness=Freshness.STALE,
            detail=detail,
        )


def _market(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 market figures; a figure in another shape is left ``None`` (or ``UNKNOWN``)."""

    usd = {name: _decimal(raw.get(name)) for name in USD_SIGNALS}
    figures: dict[str, Any] = {
        name: value if value is not None and value >= 0 else None for name, value in usd.items()
    }
    share = _decimal(raw.get("sniper_share"))
    count = raw.get("sniper_count")
    status = raw.get("developer_status")
    created = _timestamp(raw.get("created_at"))
    wash = raw.get("wash_trading")
    figures.update(
        sniper_share=share if share is not None and Decimal(0) <= share <= Decimal(1) else None,
        sniper_count=count
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else None,
        developer_status=_DEVELOPER_STATUSES.get(status) if isinstance(status, str) else None,
        created_at=created,
        wash_trading=RiskSignal.UNSAFE
        if wash is True
        else RiskSignal.SAFE
        if wash is False
        else RiskSignal.UNKNOWN,
    )
    return figures


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
