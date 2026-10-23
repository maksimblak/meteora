"""Deterministic provider fixtures for pipeline and degradation tests.

Synthetic, provider-neutral snapshots built in code - not the Meteora wire format. Tests of
the real Data API format use recorded responses in ``tests/providers/fixtures/meteora_live``.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolPage,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    ProviderState,
    RiskSignal,
    TokenRiskSnapshot,
    WalletState,
)
from src.providers.contracts import (
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class FixtureFailure(StrEnum):
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    OUTAGE = "OUTAGE"


class FixtureMarketDataProvider:
    def __init__(
        self,
        *,
        pages: Sequence[PoolPage],
        snapshots: Sequence[PoolSnapshot],
        failure: FixtureFailure | None = None,
    ) -> None:
        if not pages:
            raise ValueError("fixture market provider requires at least one page")
        self._pages = _index_pages(pages)
        self._snapshots = {snapshot.pool.address.root: snapshot for snapshot in snapshots}
        if len(self._snapshots) != len(snapshots):
            raise ValueError("fixture contains duplicate pool snapshots")
        self._failure = failure
        self._health = _fixture_health(failure)
        self.list_calls: list[str | None] = []
        self.snapshot_calls: list[str] = []

    @property
    def health(self) -> ProviderHealth:
        return self._health

    async def list_pools(self, cursor: str | None) -> PoolPage:
        self.list_calls.append(cursor)
        _raise_fixture_failure(self._failure)
        try:
            return self._pages[cursor]
        except KeyError as error:
            raise ProviderPayloadError("fixture received an unknown cursor") from error

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        del at
        self.snapshot_calls.append(pool.address.root)
        _raise_fixture_failure(self._failure)
        try:
            return self._snapshots[pool.address.root]
        except KeyError as error:
            raise ProviderPayloadError("fixture has no snapshot for pool") from error


class FixtureOnChainProvider:
    def __init__(
        self,
        *,
        token_risks: Mapping[str, TokenRiskSnapshot] | None = None,
        wallet_states: Mapping[str, WalletState] | None = None,
        failure: FixtureFailure | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._token_risks = dict(token_risks or {})
        self._wallet_states = dict(wallet_states or {})
        self._failure = failure
        self._clock = clock or SystemClock()
        self._health = _fixture_health(failure)

    @property
    def health(self) -> ProviderHealth:
        return self._health

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        if self._failure is FixtureFailure.MALFORMED_PAYLOAD:
            raise ProviderPayloadError("fixture provider malformed payload")
        if self._failure is not None:
            return _unknown_token_risk(mint, self._clock.now(), self._failure)
        try:
            return self._token_risks[mint.root]
        except KeyError as error:
            raise ProviderPayloadError("fixture has no risk snapshot for mint") from error

    async def get_wallet_state(self, wallet: PublicKey) -> WalletState:
        _raise_fixture_failure(self._failure)
        try:
            return self._wallet_states[wallet.root]
        except KeyError as error:
            raise ProviderPayloadError("fixture has no state for wallet") from error


def _index_pages(pages: Sequence[PoolPage]) -> dict[str | None, PoolPage]:
    indexed: dict[str | None, PoolPage] = {None: pages[0]}
    for previous, page in zip(pages, pages[1:], strict=False):
        cursor = previous.next_cursor
        if cursor is None or cursor in indexed:
            raise ValueError("fixture pages must form a unique cursor chain")
        indexed[cursor] = page
    if pages[-1].next_cursor is not None:
        raise ValueError("fixture final page must terminate pagination")
    return indexed


def _raise_fixture_failure(failure: FixtureFailure | None) -> None:
    if failure is FixtureFailure.RATE_LIMIT:
        raise ProviderRateLimitError("fixture provider rate limit")
    if failure is FixtureFailure.TIMEOUT:
        raise ProviderTimeoutError("fixture provider timeout")
    if failure is FixtureFailure.MALFORMED_PAYLOAD:
        raise ProviderPayloadError("fixture provider malformed payload")
    if failure is FixtureFailure.OUTAGE:
        raise ProviderUnavailableError("fixture provider outage")


def _fixture_health(failure: FixtureFailure | None) -> ProviderHealth:
    if failure is None:
        return ProviderHealth(ProviderState.HEALTHY, 0, None, None)
    state = (
        ProviderState.DEGRADED
        if failure is FixtureFailure.MALFORMED_PAYLOAD
        else ProviderState.OPEN
    )
    return ProviderHealth(state, 1, failure.value.lower(), None)


def _unknown_token_risk(
    mint: PublicKey,
    now: datetime,
    failure: FixtureFailure,
) -> TokenRiskSnapshot:
    quality = (
        DataQuality.INVALID
        if failure is FixtureFailure.MALFORMED_PAYLOAD
        else DataQuality.UNAVAILABLE
    )
    return TokenRiskSnapshot(
        mint=mint,
        mint_authority=AuthorityStatus.UNKNOWN,
        freeze_authority=AuthorityStatus.UNKNOWN,
        blacklisted=RiskSignal.UNKNOWN,
        verified=RiskSignal.UNKNOWN,
        holder_concentration=None,
        wash_trading=RiskSignal.UNKNOWN,
        observed_at=now,
        source_at=now,
        ingested_at=now,
        freshness=Freshness.STALE,
        data_quality=quality,
        missing_signals=(
            "blacklisted",
            "freeze_authority",
            "holder_concentration",
            "mint_authority",
            "verified",
            "wash_trading",
        ),
        provider="fixture_on_chain",
    )
