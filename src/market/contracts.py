"""Stable provider and storage ports for the market-data domain."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.domain.models.primitives import PublicKey
from src.market.models import (
    PoolPage,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    TokenRiskSnapshot,
    VolumeBucket,
    WalletState,
)


class MarketDataProvider(Protocol):
    @property
    def health(self) -> ProviderHealth: ...

    async def list_pools(self, cursor: str | None) -> PoolPage: ...

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot: ...


class VolumeHistorySource(Protocol):
    """Closed short-window buckets of one pool, to warm up a new candidate (D07)."""

    async def volume_history(
        self, pool: PoolRef, *, start: datetime, end: datetime
    ) -> tuple[VolumeBucket, ...]: ...


class OnChainProvider(Protocol):
    @property
    def health(self) -> ProviderHealth: ...

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot: ...

    async def get_wallet_state(self, wallet: PublicKey) -> WalletState: ...


class UniverseSnapshotStage(Protocol):
    async def stage(self, snapshots: tuple[PoolSnapshot, ...]) -> None: ...

    async def commit(self) -> int: ...

    async def abort(self) -> None: ...


class UniverseSnapshotStore(Protocol):
    async def begin(
        self,
        sync_id: UUID,
        observed_at: datetime,
    ) -> UniverseSnapshotStage: ...
