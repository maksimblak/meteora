import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    DataQuality,
    Freshness,
    PoolPage,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    ProviderState,
    WindowMetrics,
)
from src.market.universe import InMemoryUniverseStore, SyncStatus, UniverseSync
from src.providers.contracts import ProviderUnavailableError

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
ADDRESSES = (
    PublicKey("11111111111111111111111111111111"),
    PublicKey("So11111111111111111111111111111111111111112"),
    PublicKey("SysvarRent111111111111111111111111111111111"),
)


def _pool(index: int) -> PoolRef:
    return PoolRef(
        address=ADDRESSES[index],
        base_mint=ADDRESSES[(index + 1) % 3],
        quote_mint=ADDRESSES[(index + 2) % 3],
    )


def _snapshot(pool: PoolRef, observed_at: datetime = NOW) -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal("100"), fees=Decimal("1"))
        for name in ("5m", "30m", "1h", "4h", "24h")
    }
    return PoolSnapshot(
        pool=pool,
        windows=windows,
        tvl=Decimal("1000"),
        depth=Decimal("500"),
        price=Decimal("2"),
        bin_step=25,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        observed_at=observed_at,
        source_at=observed_at,
        ingested_at=observed_at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


class RecordingProvider:
    def __init__(self) -> None:
        self.pools = tuple(_pool(index) for index in range(3))
        self.list_calls: list[str | None] = []
        self.snapshot_calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.fail_address: str | None = None
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)
        self.events: list[str] = []

    async def list_pools(self, cursor: str | None) -> PoolPage:
        self.list_calls.append(cursor)
        self.events.append(f"list:{cursor}")
        if cursor is None:
            return PoolPage(items=self.pools[:2], next_cursor="last")
        return PoolPage(items=self.pools[2:], next_cursor=None)

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        self.snapshot_calls.append(pool.address.root)
        self.events.append(f"snapshot:{pool.address.root}")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if pool.address.root == self.fail_address:
            raise ProviderUnavailableError("fixture outage")
        return _snapshot(pool, at)


class RecordingStage:
    def __init__(self, store: "RecordingStagingStore") -> None:
        self._store = store

    async def stage(self, snapshots: tuple[PoolSnapshot, ...]) -> None:
        self._store.stage_sizes.append(len(snapshots))
        self._store.events.append("stage")

    async def commit(self) -> int:
        self._store.commit_calls += 1
        self._store.events.append("commit")
        return 0

    async def abort(self) -> None:
        self._store.abort_calls += 1
        self._store.events.append("abort")


class RecordingStagingStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stage_sizes: list[int] = []
        self.begin_calls = 0
        self.commit_calls = 0
        self.abort_calls = 0
        self.legacy_publish_called = False

    async def begin(self, sync_id: UUID, observed_at: datetime) -> RecordingStage:
        del sync_id, observed_at
        self.begin_calls += 1
        self.events.append("begin")
        return RecordingStage(self)

    async def publish(
        self,
        sync_id: UUID,
        observed_at: datetime,
        snapshots: tuple[PoolSnapshot, ...],
    ) -> int:
        del sync_id, observed_at, snapshots
        self.legacy_publish_called = True
        return 0


def test_universe_sync_is_complete_atomic_bounded_and_idempotent() -> None:
    async def exercise() -> None:
        provider = RecordingProvider()
        store = InMemoryUniverseStore()
        clock = FrozenClock(NOW)
        sync = UniverseSync(
            provider,
            store,
            clock=clock,
            max_concurrency=2,
            priority_addresses={provider.pools[1].address.root},
        )

        first = await sync.run(UUID(int=1))

        assert first.status is SyncStatus.COMPLETED
        assert first.pool_count == 3
        assert provider.list_calls == [None, "last"]
        assert provider.snapshot_calls[0] == provider.pools[1].address.root
        assert provider.max_active == 2
        assert store.current is not None
        assert len(store.current.snapshots) == 3

        second = await sync.run(UUID(int=2))
        assert second.status is SyncStatus.COMPLETED
        assert second.duplicate_count == 3
        assert all(len(store.history_for(pool.address)) == 1 for pool in provider.pools)

        completed_version = store.current
        clock.advance(timedelta(minutes=5))
        provider.fail_address = provider.pools[2].address.root
        failed = await sync.run(UUID(int=3))

        assert failed.status is SyncStatus.FAILED
        assert failed.error_code == "PROVIDER_UNAVAILABLE"
        assert store.current == completed_version
        assert all(len(store.history_for(pool.address)) == 1 for pool in provider.pools)

    asyncio.run(exercise())


def test_universe_sync_limits_each_snapshot_batch() -> None:
    async def exercise() -> None:
        provider = RecordingProvider()
        store = RecordingStagingStore(provider.events)
        sync = UniverseSync(
            provider,
            store,
            clock=FrozenClock(NOW),
            max_concurrency=2,
            batch_size=1,
            priority_addresses={provider.pools[1].address.root},
        )

        result = await sync.run(UUID(int=4))

        assert result.status is SyncStatus.COMPLETED
        assert provider.max_active == 1
        assert store.begin_calls == 1
        assert store.stage_sizes == [1, 1, 1]
        assert store.commit_calls == 1
        assert store.abort_calls == 0
        assert store.legacy_publish_called is False
        assert provider.events.index(f"snapshot:{provider.pools[0].address.root}") < (
            provider.events.index("list:last")
        )
        assert provider.events.index("stage") < provider.events.index("list:last")

    asyncio.run(exercise())
