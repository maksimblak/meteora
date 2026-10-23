import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import DataQuality, Freshness, PoolRef
from src.providers.contracts import ProviderPayloadError
from src.providers.meteora import MeteoraDataApiProvider
from src.providers.resilience import ProviderHealth, ProviderState

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
POOL = "SysvarRent111111111111111111111111111111111"
MINT_A = "11111111111111111111111111111111"
MINT_B = "So11111111111111111111111111111111111111112"


class StubJsonClient:
    def __init__(self, responses: dict[tuple[str, tuple[tuple[str, str], ...]], Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        normalized = params or {}
        self.calls.append((path, normalized))
        return self.responses[(path, tuple(sorted(normalized.items())))]


def test_meteora_adapter_paginates_and_normalizes_exact_windows() -> None:
    async def exercise() -> None:
        client = StubJsonClient(
            {
                ("/pools", ()): {
                    "items": [
                        {
                            "address": POOL,
                            "base_mint": MINT_A,
                            "quote_mint": MINT_B,
                        }
                    ],
                    "next_cursor": "second-page",
                },
                ("/pools", (("cursor", "second-page"),)): {
                    "items": [],
                    "next_cursor": None,
                },
                (f"/pools/{POOL}", ()): {
                    "source_at": "2026-10-18T09:59:45Z",
                    "created_at": "2026-10-01T00:00:00Z",
                    "tvl": "1234.567890123456789",
                    "depth": "900.01",
                    "price": "0.00000000123456789",
                    "bin_step": 25,
                    "windows": {
                        "5m": {"volume": "10", "fees": "0.01"},
                        "30m": {"volume": "50", "fees": "0.05"},
                        "1h": {"volume": "100", "fees": "0.1"},
                        "4h": {"volume": "400", "fees": "0.4"},
                        "24h": {"volume": "2400", "fees": "2.4"},
                    },
                },
            }
        )
        provider = MeteoraDataApiProvider(
            client,
            clock=FrozenClock(NOW),
            stale_after=timedelta(minutes=2),
        )

        first = await provider.list_pools(None)
        second = await provider.list_pools(first.next_cursor)
        snapshot = await provider.get_pool_snapshot(first.items[0], NOW)

        assert first.next_cursor == "second-page"
        assert second.items == ()
        assert snapshot.windows["5m"].volume == Decimal("10")
        assert snapshot.windows["24h"].fees == Decimal("2.4")
        assert snapshot.tvl == Decimal("1234.567890123456789")
        assert snapshot.freshness is Freshness.FRESH
        assert provider.health == client.health
        assert client.calls == [
            ("/pools", {}),
            ("/pools", {"cursor": "second-page"}),
            (f"/pools/{POOL}", {}),
        ]

    asyncio.run(exercise())


def _guide_payload(**extra: Any) -> dict[str, Any]:
    return {
        "source_at": "2026-10-18T09:59:45Z",
        "created_at": "2026-10-18T06:00:00Z",
        "tvl": "87000",
        "depth": "40000",
        "price": "0.0021",
        "bin_step": 100,
        "windows": {
            name: {"volume": "1000", "fees": "10"} for name in ("5m", "30m", "1h", "4h", "24h")
        },
        **extra,
    }


def test_meteora_adapter_reads_base_fee_and_base_token_market_cap() -> None:
    """The live Data API nests both: ``pool_config.base_fee_pct`` and ``token_x.market_cap``."""

    async def exercise() -> None:
        pool = PoolRef(
            address=PublicKey(POOL),
            base_mint=PublicKey(MINT_A),
            quote_mint=PublicKey(MINT_B),
        )
        nested = _guide_payload(
            pool_config={"bin_step": 100, "base_fee_pct": "1"},
            token_x={"address": MINT_A, "market_cap": "2100000"},
            token_y={"address": MINT_B, "market_cap": "61448457325"},
        )
        swapped = _guide_payload(
            base_fee_percentage="0.25",
            token_x={"address": MINT_B, "market_cap": "61448457325"},
            token_y={"address": MINT_A, "market_cap": "750000"},
        )
        bare = _guide_payload()
        provider_for = {
            name: MeteoraDataApiProvider(
                StubJsonClient({(f"/pools/{POOL}", ()): payload}), clock=FrozenClock(NOW)
            )
            for name, payload in (("nested", nested), ("swapped", swapped), ("bare", bare))
        }

        nested_snapshot = await provider_for["nested"].get_pool_snapshot(pool, NOW)
        swapped_snapshot = await provider_for["swapped"].get_pool_snapshot(pool, NOW)
        bare_snapshot = await provider_for["bare"].get_pool_snapshot(pool, NOW)

        assert nested_snapshot.base_fee_rate == Decimal("0.01")
        assert nested_snapshot.base_token_market_cap == Decimal("2100000")
        assert swapped_snapshot.base_fee_rate == Decimal("0.0025")
        assert swapped_snapshot.base_token_market_cap == Decimal("750000")
        assert bare_snapshot.base_fee_rate is None
        assert bare_snapshot.base_token_market_cap is None
        assert bare_snapshot.data_quality is DataQuality.COMPLETE

    asyncio.run(exercise())


def test_meteora_adapter_rejects_impossible_guide_fields() -> None:
    async def exercise() -> None:
        pool = PoolRef(
            address=PublicKey(POOL),
            base_mint=PublicKey(MINT_A),
            quote_mint=PublicKey(MINT_B),
        )
        for payload in (
            _guide_payload(base_fee_pct="-1"),
            _guide_payload(base_fee_pct="101"),
            _guide_payload(market_cap="-5"),
            _guide_payload(base_fee_pct=0.25),
        ):
            provider = MeteoraDataApiProvider(
                StubJsonClient({(f"/pools/{POOL}", ()): payload}), clock=FrozenClock(NOW)
            )
            with pytest.raises(ProviderPayloadError):
                await provider.get_pool_snapshot(pool, NOW)

    asyncio.run(exercise())


def test_meteora_adapter_exposes_stale_partial_and_rejects_invalid_payload() -> None:
    async def exercise() -> None:
        pool = PoolRef(
            address=PublicKey(POOL),
            base_mint=PublicKey(MINT_A),
            quote_mint=PublicKey(MINT_B),
        )
        base_payload: dict[str, Any] = {
            "source_at": "2026-10-18T09:55:00Z",
            "created_at": "2026-10-01T00:00:00Z",
            "tvl": "100",
            "price": "1",
            "bin_step": 25,
            "windows": {
                name: {"volume": "10", "fees": "1"} for name in ("5m", "30m", "1h", "4h", "24h")
            },
        }
        stale_client = StubJsonClient({(f"/pools/{POOL}", ()): base_payload})
        stale_provider = MeteoraDataApiProvider(
            stale_client,
            clock=FrozenClock(NOW),
            stale_after=timedelta(minutes=2),
        )

        stale = await stale_provider.get_pool_snapshot(pool, NOW)

        assert stale.freshness is Freshness.STALE
        assert stale.data_quality is DataQuality.PARTIAL
        assert stale.missing_fields == ("depth",)

        invalid_payload = {**base_payload, "tvl": "-1"}
        invalid_client = StubJsonClient({(f"/pools/{POOL}", ()): invalid_payload})
        invalid_provider = MeteoraDataApiProvider(
            invalid_client,
            clock=FrozenClock(NOW),
        )
        with pytest.raises(ProviderPayloadError, match="invalid snapshot"):
            await invalid_provider.get_pool_snapshot(pool, NOW)

    asyncio.run(exercise())
