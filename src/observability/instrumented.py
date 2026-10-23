"""Provider decorators that measure latency and normalized error codes."""

import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from src.domain.models.primitives import PublicKey
from src.market.contracts import MarketDataProvider, OnChainProvider
from src.market.models import PoolPage, PoolRef, PoolSnapshot, ProviderHealth, TokenRiskSnapshot
from src.market.models import WalletState as WalletStateModel
from src.observability.metrics import WorkerMetrics
from src.providers.contracts import (
    ProviderError,
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def provider_error_code(error: BaseException) -> str:
    if isinstance(error, ProviderRateLimitError):
        return "PROVIDER_RATE_LIMIT"
    if isinstance(error, ProviderTimeoutError):
        return "PROVIDER_TIMEOUT"
    if isinstance(error, ProviderPayloadError):
        return "PROVIDER_PAYLOAD"
    if isinstance(error, ProviderUnavailableError):
        return "PROVIDER_UNAVAILABLE"
    if isinstance(error, ProviderError):
        return "PROVIDER_ERROR"
    return "UNEXPECTED_ERROR"


async def _measured[T](
    metrics: WorkerMetrics,
    provider: str,
    operation: str,
    health: Callable[[], ProviderHealth],
    call: Callable[[], Awaitable[T]],
) -> T:
    started = time.perf_counter()
    try:
        result = await call()
    except Exception as error:
        metrics.provider_request(
            provider, operation, time.perf_counter() - started, provider_error_code(error)
        )
        metrics.provider_state(provider, health())
        raise
    metrics.provider_request(provider, operation, time.perf_counter() - started, "ok")
    metrics.provider_state(provider, health())
    return result


class InstrumentedMarketDataProvider:
    def __init__(self, inner: MarketDataProvider, metrics: WorkerMetrics, *, name: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._name = name

    @property
    def health(self) -> ProviderHealth:
        return self._inner.health

    async def list_pools(self, cursor: str | None) -> PoolPage:
        return await _measured(
            self._metrics,
            self._name,
            "list_pools",
            lambda: self._inner.health,
            lambda: self._inner.list_pools(cursor),
        )

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        return await _measured(
            self._metrics,
            self._name,
            "get_pool_snapshot",
            lambda: self._inner.health,
            lambda: self._inner.get_pool_snapshot(pool, at),
        )


class InstrumentedOnChainProvider:
    def __init__(self, inner: OnChainProvider, metrics: WorkerMetrics, *, name: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._name = name

    @property
    def health(self) -> ProviderHealth:
        return self._inner.health

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        return await _measured(
            self._metrics,
            self._name,
            "get_token_risk",
            lambda: self._inner.health,
            lambda: self._inner.get_token_risk(mint),
        )

    async def get_wallet_state(self, wallet: PublicKey) -> WalletStateModel:
        return await _measured(
            self._metrics,
            self._name,
            "get_wallet_state",
            lambda: self._inner.health,
            lambda: self._inner.get_wallet_state(wallet),
        )
