import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    ProviderState,
    RiskSignal,
)
from src.providers.contracts import ProviderTimeoutError
from src.providers.solana import SolanaRpcProvider

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
MINT = PublicKey("11111111111111111111111111111111")
FREEZE_AUTHORITY = "So11111111111111111111111111111111111111112"
MINT_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "context": {"slot": 123},
        "value": {
            "data": {
                "parsed": {
                    "info": {
                        "mintAuthority": None,
                        "freezeAuthority": FREEZE_AUTHORITY,
                    }
                }
            }
        },
    },
}


class StubRpcClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def post_json(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_solana_rpc_maps_authorities_and_keeps_unavailable_signals_unknown() -> None:
    async def exercise() -> None:
        client = StubRpcClient(MINT_RESPONSE)
        provider = SolanaRpcProvider(client, clock=FrozenClock(NOW))

        risk = await provider.get_token_risk(MINT)

        assert risk.mint_authority is AuthorityStatus.REVOKED
        assert risk.freeze_authority is AuthorityStatus.PRESENT
        assert risk.blacklisted is RiskSignal.UNKNOWN
        assert risk.verified is RiskSignal.UNKNOWN
        assert risk.wash_trading is RiskSignal.UNKNOWN
        assert risk.data_quality is DataQuality.PARTIAL
        assert set(risk.missing_signals) == {
            "blacklisted",
            "holder_concentration",
            "verified",
            "wash_trading",
        }
        assert client.requests == [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [MINT.root, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            }
        ]

    asyncio.run(exercise())


def test_enhanced_provider_outage_does_not_turn_unknown_into_safe() -> None:
    class OutageEnhancedProvider:
        async def get_token_signals(self, mint: PublicKey) -> Any:
            raise ProviderTimeoutError("fixture enhanced timeout")

    async def exercise() -> None:
        provider = SolanaRpcProvider(
            StubRpcClient(MINT_RESPONSE),
            clock=FrozenClock(NOW),
            enhanced_provider=OutageEnhancedProvider(),
        )

        risk = await provider.get_token_risk(MINT)

        assert risk.mint_authority is AuthorityStatus.REVOKED
        assert risk.freeze_authority is AuthorityStatus.PRESENT
        assert risk.blacklisted is RiskSignal.UNKNOWN
        assert risk.verified is RiskSignal.UNKNOWN
        assert risk.wash_trading is RiskSignal.UNKNOWN
        assert risk.data_quality is DataQuality.PARTIAL

    asyncio.run(exercise())


def test_stale_enhanced_risk_remains_stale_after_rpc_merge() -> None:
    source_at = NOW - timedelta(minutes=10)

    class StaleEnhancedProvider:
        async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
            return EnhancedTokenSignals(
                blacklisted=RiskSignal.SAFE,
                verified=RiskSignal.SAFE,
                wash_trading=RiskSignal.SAFE,
                holder_concentration=Decimal("0.1"),
                provider="fixture_enhanced",
                freshness=Freshness.STALE,
                source_at=source_at,
            )

    async def exercise() -> None:
        provider = SolanaRpcProvider(
            StubRpcClient(MINT_RESPONSE),
            clock=FrozenClock(NOW),
            enhanced_provider=StaleEnhancedProvider(),
        )

        risk = await provider.get_token_risk(MINT)

        assert risk.freshness is Freshness.STALE
        assert risk.source_at == source_at
        assert risk.data_quality is DataQuality.COMPLETE

    asyncio.run(exercise())


def test_solana_rpc_outage_returns_explicit_unknown_risk() -> None:
    async def exercise() -> None:
        provider = SolanaRpcProvider(
            StubRpcClient(ProviderTimeoutError("fixture timeout")),
            clock=FrozenClock(NOW),
        )

        risk = await provider.get_token_risk(MINT)

        assert risk.mint_authority is AuthorityStatus.UNKNOWN
        assert risk.freeze_authority is AuthorityStatus.UNKNOWN
        assert risk.blacklisted is RiskSignal.UNKNOWN
        assert risk.freshness is Freshness.STALE
        assert risk.data_quality is DataQuality.UNAVAILABLE
        assert set(risk.missing_signals) == {
            "blacklisted",
            "freeze_authority",
            "holder_concentration",
            "mint_authority",
            "verified",
            "wash_trading",
        }

    asyncio.run(exercise())
