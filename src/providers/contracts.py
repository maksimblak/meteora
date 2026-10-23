"""Small transport boundary shared by provider adapters."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.domain.models.primitives import PublicKey
from src.market.models import EnhancedTokenSignals, PoolRef, ProviderHealth


@dataclass(frozen=True, slots=True)
class LbPairState:
    """On-chain facts read from a DLMM pair's LB pair account (spec 8.5, D06)."""

    active_bin_id: int
    bin_step: int
    token_x_mint: str
    token_y_mint: str


class PoolStateReader(Protocol):
    """Fills what the Data API does not report: the active bin and, if missing, decimals."""

    async def get_pool_states(self, pools: Sequence[PoolRef]) -> Mapping[str, LbPairState]: ...

    async def get_mint_decimals(self, mints: Sequence[PublicKey]) -> Mapping[str, int]: ...


class JsonClient(Protocol):
    @property
    def health(self) -> ProviderHealth: ...

    async def get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any: ...


class RpcJsonClient(Protocol):
    @property
    def health(self) -> ProviderHealth: ...

    async def post_json(self, payload: dict[str, Any]) -> Any: ...


class EnhancedTokenIntelligenceProvider(Protocol):
    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals: ...


class ProviderError(RuntimeError):
    """Base error for an external provider failure."""


class ProviderPayloadError(ProviderError):
    """The provider returned a response that cannot be normalized safely."""


class ProviderUnavailableError(ProviderError):
    """The provider is unavailable after bounded retries or breaker rejection."""


class ProviderRateLimitError(ProviderUnavailableError):
    """The provider rejected a request due to rate limiting."""


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider request exceeded its configured timeout."""
