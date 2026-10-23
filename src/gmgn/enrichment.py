"""Token risk of a candidate with its GMGN reading attached (G18).

The candidate pipeline reads token risk only for candidates (after the cheap filters), so
wrapping the on-chain provider it uses is what keeps GMGN page loads to candidates. The
wrapper asks both sources at once and attaches the reading with ``merge_enhanced_signals``.
"""

import asyncio
from typing import Protocol

from src.core.clock import Clock
from src.domain.models.primitives import PublicKey
from src.domain.screening import merge_enhanced_signals
from src.market.contracts import OnChainProvider
from src.market.models import (
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    TokenRiskSnapshot,
    WalletState,
)


class EnhancedSignalSource(Protocol):
    """``GmgnProvider`` as the worker uses it: signals per mint and normalized health."""

    @property
    def health(self) -> ProviderHealth: ...

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals: ...


class EnrichedOnChainProvider:
    """An ``OnChainProvider`` whose token risk carries the enhanced source's reading.

    A failure of the on-chain read propagates unchanged (the pipeline already turns it
    into an unknown token). A failure of the enhanced source never does: it becomes an
    unavailable reading, so the decision shows ``GMGN_UNAVAILABLE`` instead of failing.
    """

    def __init__(
        self, onchain: OnChainProvider, enhanced: EnhancedSignalSource, *, clock: Clock
    ) -> None:
        self._onchain = onchain
        self._enhanced = enhanced
        self._clock = clock

    @property
    def health(self) -> ProviderHealth:
        return self._onchain.health

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        risk, signals = await asyncio.gather(
            self._onchain.get_token_risk(mint),
            self._enhanced.get_token_signals(mint),
            return_exceptions=True,
        )
        if isinstance(risk, BaseException):
            raise risk
        if isinstance(signals, BaseException):
            if not isinstance(signals, Exception):
                raise signals
            signals = EnhancedTokenSignals(
                provider="gmgn",
                source_at=self._clock.now(),
                freshness=Freshness.STALE,
                detail="PROVIDER_ERROR",
            )
        return merge_enhanced_signals(risk, signals)

    async def get_wallet_state(self, wallet: PublicKey) -> WalletState:
        return await self._onchain.get_wallet_state(wallet)
