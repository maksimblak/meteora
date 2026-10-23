"""Cursor-based universe ingestion with atomic publication and idempotent history."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import PublicKey
from src.market.contracts import (
    MarketDataProvider,
    UniverseSnapshotStage,
    UniverseSnapshotStore,
)
from src.market.models import PoolRef, PoolSnapshot
from src.market.windows import ShortWindowRules, derive_short_window
from src.providers.contracts import (
    ProviderError,
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class PreviousUniverse(Protocol):
    """The last published universe: the predecessor of each pool's next snapshot."""

    async def latest_universe(self) -> tuple[PoolSnapshot, ...]: ...


class SyncStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SyncResult:
    sync_id: UUID
    status: SyncStatus
    pool_count: int
    duplicate_count: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseVersion:
    sync_id: UUID
    observed_at: datetime
    snapshots: tuple[PoolSnapshot, ...]


class InMemoryUniverseStore:
    """Test seam whose staging commit has the same all-or-nothing boundary as storage."""

    def __init__(self) -> None:
        self._current: UniverseVersion | None = None
        self._history: dict[str, dict[datetime, PoolSnapshot]] = {}
        self._lock = asyncio.Lock()

    @property
    def current(self) -> UniverseVersion | None:
        return self._current

    def history_for(self, address: PublicKey) -> tuple[PoolSnapshot, ...]:
        history = self._history.get(address.root, {})
        return tuple(history[instant] for instant in sorted(history))

    async def begin(
        self,
        sync_id: UUID,
        observed_at: datetime,
    ) -> UniverseSnapshotStage:
        return _InMemoryUniverseStage(self, sync_id, observed_at)


class _InMemoryUniverseStage:
    def __init__(
        self,
        store: InMemoryUniverseStore,
        sync_id: UUID,
        observed_at: datetime,
    ) -> None:
        self._store = store
        self._sync_id = sync_id
        self._observed_at = observed_at
        self._snapshots: list[PoolSnapshot] = []
        self._addresses: set[str] = set()
        self._closed = False

    async def stage(self, snapshots: tuple[PoolSnapshot, ...]) -> None:
        self._ensure_open()
        for snapshot in snapshots:
            address = snapshot.pool.address.root
            if address in self._addresses:
                raise ValueError("universe version contains duplicate pools")
            if snapshot.observed_at != self._observed_at:
                raise ValueError("universe version contains mixed observation times")
            self._addresses.add(address)
            self._snapshots.append(snapshot)

    async def commit(self) -> int:
        self._ensure_open()
        snapshots = tuple(self._snapshots)

        async with self._store._lock:
            next_history = {
                address: dict(observations)
                for address, observations in self._store._history.items()
            }
            duplicates = 0
            for snapshot in snapshots:
                address = snapshot.pool.address.root
                observations = next_history.setdefault(address, {})
                if snapshot.observed_at in observations:
                    duplicates += 1
                else:
                    observations[snapshot.observed_at] = snapshot
            version = UniverseVersion(self._sync_id, self._observed_at, snapshots)
            self._store._history = next_history
            self._store._current = version
        self._closed = True
        self._snapshots.clear()
        self._addresses.clear()
        return duplicates

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._snapshots.clear()
        self._addresses.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("universe snapshot stage is already closed")


class UniverseSync:
    def __init__(
        self,
        provider: MarketDataProvider,
        store: UniverseSnapshotStore,
        *,
        clock: Clock | None = None,
        max_concurrency: int = 8,
        batch_size: int | None = None,
        priority_addresses: set[str] | None = None,
        previous: PreviousUniverse | None = None,
        short_window: ShortWindowRules | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be at least one")
        self._provider = provider
        self._store = store
        self._clock = clock or SystemClock()
        self._max_concurrency = max_concurrency
        self._batch_size = batch_size or max_concurrency
        self._priority_addresses = frozenset(priority_addresses or ())
        self._previous = previous
        self._short_window = short_window

    async def run(self, sync_id: UUID) -> SyncResult:
        """List every page, then observe the version and stage it in bounded batches.

        The instant is taken after the last page: a source that serves only the present
        (the Data API) stamps each pool with its page's arrival, and no snapshot may hold
        data newer than its ``observed_at``. A listing failure opens no version at all.
        """

        try:
            pools = await self._list_universe()
        except ProviderError as error:
            return _failed(sync_id, error)
        previous = await self._previous_snapshots()
        observed_at = self._clock.now()
        stage = await self._store.begin(sync_id, observed_at)
        try:
            pool_count = await self._stage_snapshots(stage, pools, observed_at, previous)
            duplicates = await stage.commit()
        except ProviderError as error:
            await stage.abort()
            return _failed(sync_id, error)
        except BaseException:
            await stage.abort()
            raise
        return SyncResult(
            sync_id=sync_id,
            status=SyncStatus.COMPLETED,
            pool_count=pool_count,
            duplicate_count=duplicates,
        )

    async def _list_universe(self) -> tuple[PoolRef, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_pools: set[str] = set()
        priority: list[PoolRef] = []
        regular: list[PoolRef] = []
        while True:
            page = await self._provider.list_pools(cursor)
            for pool in page.items:
                address = pool.address.root
                if address in seen_pools:
                    raise ProviderPayloadError("provider returned duplicate or mixed pools")
                seen_pools.add(address)
                (priority if address in self._priority_addresses else regular).append(pool)
            if page.next_cursor is None:
                return (*priority, *regular)
            if page.next_cursor in seen_cursors:
                raise ProviderPayloadError("provider returned a pagination cursor cycle")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    async def _previous_snapshots(self) -> dict[str, PoolSnapshot]:
        """Each pool's snapshot in the last published universe, its short-window base."""

        if self._previous is None or self._short_window is None:
            return {}
        return {
            snapshot.pool.address.root: snapshot
            for snapshot in await self._previous.latest_universe()
        }

    def _derive(
        self, snapshots: tuple[PoolSnapshot, ...], previous: dict[str, PoolSnapshot]
    ) -> tuple[PoolSnapshot, ...]:
        rules = self._short_window
        if rules is None:
            return snapshots
        return tuple(
            derive_short_window(snapshot, previous.get(snapshot.pool.address.root), rules)
            for snapshot in snapshots
        )

    async def _stage_snapshots(
        self,
        stage: UniverseSnapshotStage,
        pools: tuple[PoolRef, ...],
        observed_at: datetime,
        previous: dict[str, PoolSnapshot],
    ) -> int:
        pool_count = 0
        for offset in range(0, len(pools), self._batch_size):
            batch = pools[offset : offset + self._batch_size]
            snapshots = self._derive(await self._snapshot_batch(batch, observed_at), previous)
            try:
                await stage.stage(snapshots)
            except ValueError as error:
                raise ProviderPayloadError("provider returned duplicate or mixed pools") from error
            pool_count += len(snapshots)
        return pool_count

    async def _snapshot_batch(
        self, pools: tuple[PoolRef, ...], observed_at: datetime
    ) -> tuple[PoolSnapshot, ...]:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def load(pool: PoolRef) -> PoolSnapshot:
            async with semaphore:
                return await self._provider.get_pool_snapshot(pool, observed_at)

        results = await asyncio.gather(*(load(pool) for pool in pools), return_exceptions=True)
        snapshots: list[PoolSnapshot] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, ProviderError):
                raise result
            if isinstance(result, BaseException):
                raise ProviderUnavailableError("market provider failed") from result
            snapshots.append(result)
        return tuple(snapshots)


def _failed(sync_id: UUID, error: ProviderError) -> SyncResult:
    return SyncResult(
        sync_id=sync_id,
        status=SyncStatus.FAILED,
        pool_count=0,
        error_code=_provider_error_code(error),
    )


def _provider_error_code(error: ProviderError) -> str:
    if isinstance(error, ProviderRateLimitError):
        return "PROVIDER_RATE_LIMIT"
    if isinstance(error, ProviderTimeoutError):
        return "PROVIDER_TIMEOUT"
    if isinstance(error, ProviderPayloadError):
        return "PROVIDER_PAYLOAD"
    if isinstance(error, ProviderUnavailableError):
        return "PROVIDER_UNAVAILABLE"
    return "PROVIDER_ERROR"
