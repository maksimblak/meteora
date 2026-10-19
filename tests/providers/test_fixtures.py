import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolPage,
    PoolRef,
    PoolSnapshot,
    WindowMetrics,
)
from src.providers.contracts import (
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from src.providers.fixtures import (
    FixtureFailure,
    FixtureMarketDataProvider,
    FixtureOnChainProvider,
)

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


def _snapshot(pool: PoolRef) -> PoolSnapshot:
    return PoolSnapshot(
        pool=pool,
        windows={
            name: WindowMetrics(volume=Decimal("10"), fees=Decimal("1"))
            for name in ("5m", "30m", "1h", "4h", "24h")
        },
        tvl=Decimal("100"),
        depth=Decimal("50"),
        price=Decimal("1"),
        bin_step=25,
        created_at=NOW,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def test_fixture_market_provider_replays_pagination_stale_and_failures() -> None:
    async def exercise() -> None:
        first_pool = _pool(0)
        second_pool = _pool(1)
        stale = _snapshot(first_pool).model_copy(update={"freshness": Freshness.STALE})
        healthy = _snapshot(second_pool)
        provider = FixtureMarketDataProvider(
            pages=(
                PoolPage(items=(first_pool,), next_cursor="page-2"),
                PoolPage(items=(second_pool,), next_cursor=None),
            ),
            snapshots=(stale, healthy),
        )

        first = await provider.list_pools(None)
        second = await provider.list_pools(first.next_cursor)
        returned = await provider.get_pool_snapshot(first_pool, NOW)

        assert first.items == (first_pool,)
        assert second.items == (second_pool,)
        assert returned.freshness is Freshness.STALE

        expected = {
            FixtureFailure.RATE_LIMIT: ProviderRateLimitError,
            FixtureFailure.TIMEOUT: ProviderTimeoutError,
            FixtureFailure.MALFORMED_PAYLOAD: ProviderPayloadError,
            FixtureFailure.OUTAGE: ProviderUnavailableError,
        }
        for failure, error_type in expected.items():
            failing = FixtureMarketDataProvider(
                pages=(PoolPage(items=(), next_cursor=None),),
                snapshots=(),
                failure=failure,
            )
            with pytest.raises(error_type):
                await failing.list_pools(None)

    asyncio.run(exercise())


def test_fixture_on_chain_outage_is_explicit_unknown() -> None:
    async def exercise() -> None:
        provider = FixtureOnChainProvider(
            failure=FixtureFailure.OUTAGE,
            clock=FrozenClock(NOW),
        )

        risk = await provider.get_token_risk(ADDRESSES[0])

        assert risk.mint_authority is AuthorityStatus.UNKNOWN
        assert risk.freeze_authority is AuthorityStatus.UNKNOWN
        assert risk.data_quality is DataQuality.UNAVAILABLE

    asyncio.run(exercise())


def test_fixture_on_chain_malformed_payload_matches_production_semantics() -> None:
    async def exercise() -> None:
        provider = FixtureOnChainProvider(
            failure=FixtureFailure.MALFORMED_PAYLOAD,
            clock=FrozenClock(NOW),
        )

        with pytest.raises(ProviderPayloadError, match="malformed payload"):
            await provider.get_token_risk(ADDRESSES[0])

    asyncio.run(exercise())
