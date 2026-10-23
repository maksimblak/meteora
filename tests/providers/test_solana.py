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
from src.providers.solana import (
    KNOWN_LIQUIDITY_PROGRAMS,
    KNOWN_PROGRAM_AUTHORITIES,
    SolanaRpcProvider,
)

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
        # Holder and activity lookups follow the mint read; an unusable answer to them
        # (this stub echoes the mint payload) keeps those signals unknown, never safe.
        assert client.requests[0] == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [MINT.root, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        }
        assert risk.holder_concentration is None
        assert risk.earliest_activity_at is None

    asyncio.run(exercise())


_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def _key(seed: int) -> str:
    number = int.from_bytes(bytes([seed]) + bytes([7]) * 31, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    return encoded


class RoutedRpcClient:
    """Answers by JSON-RPC method, so each lookup gets its own canned payload."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.methods: list[str] = []
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def post_json(self, payload: dict[str, Any]) -> Any:
        method = payload["method"]
        self.methods.append(method)
        response = self.responses[method]
        if callable(response):
            return response(payload)
        if isinstance(response, BaseException):
            raise response
        return response


def _holder_responses(
    balances: list[tuple[str, str, int]], owner_programs: dict[str, str], supply: int
) -> dict[str, Any]:
    """``balances`` lists (token account, authority, amount); programs map authority -> owner."""

    def accounts(payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload["params"][0]
        by_account = {account: authority for account, authority, _ in balances}
        if requested and requested[0] in by_account:
            value = [
                {"data": {"parsed": {"info": {"owner": by_account[address]}}}}
                for address in requested
            ]
        else:
            value = [
                {"owner": owner_programs[address], "executable": False} for address in requested
            ]
        return {"jsonrpc": "2.0", "id": 1, "result": {"value": value}}

    return {
        "getAccountInfo": MINT_RESPONSE,
        "getTokenSupply": {"result": {"value": {"amount": str(supply), "decimals": 0}}},
        "getTokenLargestAccounts": {
            "result": {
                "value": [
                    {"address": account, "amount": str(amount)}
                    for account, _, amount in sorted(balances, key=lambda item: -item[2])
                ]
            }
        },
        "getMultipleAccounts": accounts,
        "getSignaturesForAddress": {"result": []},
    }


def test_top_ten_share_excludes_pool_vaults_and_programs_and_merges_wallet_accounts() -> None:
    pool_authority, curve_authority = _key(1), _key(2)
    wallets = [_key(10 + index) for index in range(11)]
    amounts = [25_000, 20_000, 15_000, 10_000, 10_000, 5_000, 5_000, 5_000, 5_000, 4_000]
    balances = [
        (_key(100), pool_authority, 400_000),  # DLMM reserve: owned by the pair PDA
        (_key(101), curve_authority, 200_000),  # launchpad bonding curve
        (_key(102), wallets[0], 20_000),
        (_key(103), wallets[0], 10_000),  # the same wallet's second token account
        *((_key(110 + index), wallets[index + 1], amount) for index, amount in enumerate(amounts)),
    ]
    programs = {
        pool_authority: DLMM_PROGRAM,
        curve_authority: PUMP_PROGRAM,
        **{wallet: SYSTEM_PROGRAM for wallet in wallets},
    }

    async def exercise() -> None:
        provider = SolanaRpcProvider(
            RoutedRpcClient(_holder_responses(balances, programs, supply=1_000_000)),
            clock=FrozenClock(NOW),
        )

        risk = await provider.get_token_risk(MINT)

        # top-10 wallets: 30k + 25k + 20k + 15k + 10k + 10k + 5k + 5k + 5k + 5k = 130k of 1M
        assert risk.holder_concentration == Decimal("0.13")
        assert "holder_concentration" not in risk.missing_signals

    asyncio.run(exercise())


def test_top_ten_share_is_unknown_when_the_visible_page_cannot_prove_ten_holders() -> None:
    authority = _key(1)
    balances = [(_key(100 + index), authority, 1_000 + index) for index in range(20)]

    async def exercise() -> None:
        provider = SolanaRpcProvider(
            RoutedRpcClient(
                _holder_responses(balances, {authority: DLMM_PROGRAM}, supply=1_000_000)
            ),
            clock=FrozenClock(NOW),
        )

        risk = await provider.get_token_risk(MINT)

        assert risk.holder_concentration is None
        assert "holder_concentration" in risk.missing_signals

    asyncio.run(exercise())


def test_excluded_program_lists_are_valid_public_keys() -> None:
    for address in KNOWN_LIQUIDITY_PROGRAMS | KNOWN_PROGRAM_AUTHORITIES:
        assert PublicKey(address).root == address
    assert DLMM_PROGRAM in KNOWN_LIQUIDITY_PROGRAMS


def test_token_activity_age_is_exact_only_when_the_signature_history_ends() -> None:
    oldest = int((NOW - timedelta(hours=3)).timestamp())
    newest = int((NOW - timedelta(minutes=1)).timestamp())

    def short_history(payload: dict[str, Any]) -> dict[str, Any]:
        return {"result": [{"blockTime": newest}, {"blockTime": None}, {"blockTime": oldest}]}

    def full_page(payload: dict[str, Any]) -> dict[str, Any]:
        limit = payload["params"][1]["limit"]
        page = [{"signature": f"s{index}", "blockTime": newest} for index in range(limit)]
        return {"result": page}

    async def exercise() -> None:
        complete_client = RoutedRpcClient(
            {**_holder_responses([], {}, supply=1), "getSignaturesForAddress": short_history}
        )
        truncated_client = RoutedRpcClient(
            {**_holder_responses([], {}, supply=1), "getSignaturesForAddress": full_page}
        )
        complete_provider = SolanaRpcProvider(complete_client, clock=FrozenClock(NOW))
        truncated_provider = SolanaRpcProvider(truncated_client, clock=FrozenClock(NOW))

        complete = await complete_provider.get_token_risk(MINT)
        again = await complete_provider.get_token_risk(MINT)
        truncated = await truncated_provider.get_token_risk(MINT)

        assert complete.earliest_activity_at == NOW - timedelta(hours=3)
        assert complete.activity_history_complete is True
        assert again.earliest_activity_at == complete.earliest_activity_at
        assert complete_client.methods.count("getSignaturesForAddress") == 1  # immutable: cached
        assert truncated.earliest_activity_at == NOW - timedelta(minutes=1)
        assert truncated.activity_history_complete is False

    asyncio.run(exercise())


def test_enhanced_total_fees_are_carried_and_absent_fees_stay_unknown() -> None:
    class FeeEnhancedProvider:
        async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
            return EnhancedTokenSignals(
                blacklisted=RiskSignal.SAFE,
                verified=RiskSignal.SAFE,
                wash_trading=RiskSignal.SAFE,
                holder_concentration=Decimal("0.4"),
                total_fees_sol=Decimal("31.5"),
                provider="fixture_enhanced",
                freshness=Freshness.FRESH,
                source_at=NOW,
            )

    async def exercise() -> None:
        enhanced = SolanaRpcProvider(
            StubRpcClient(MINT_RESPONSE),
            clock=FrozenClock(NOW),
            enhanced_provider=FeeEnhancedProvider(),
        )
        plain = SolanaRpcProvider(StubRpcClient(MINT_RESPONSE), clock=FrozenClock(NOW))

        with_fees = await enhanced.get_token_risk(MINT)
        without = await plain.get_token_risk(MINT)

        assert with_fees.total_fees_sol == Decimal("31.5")
        assert with_fees.holder_concentration == Decimal("0.4")
        assert without.total_fees_sol is None

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


def test_without_an_enhanced_provider_the_rpc_read_itself_is_fresh() -> None:
    """D09: a missing optional source leaves its signals unknown; it does not age the RPC read."""

    class OutageEnhancedProvider:
        async def get_token_signals(self, mint: PublicKey) -> Any:
            raise ProviderTimeoutError("fixture enhanced timeout")

    async def exercise() -> None:
        plain = SolanaRpcProvider(StubRpcClient(MINT_RESPONSE), clock=FrozenClock(NOW))
        outage = SolanaRpcProvider(
            StubRpcClient(MINT_RESPONSE),
            clock=FrozenClock(NOW),
            enhanced_provider=OutageEnhancedProvider(),
        )

        for risk in (await plain.get_token_risk(MINT), await outage.get_token_risk(MINT)):
            assert risk.freshness is Freshness.FRESH
            assert risk.source_at == NOW
            assert {"blacklisted", "verified", "wash_trading"} <= set(risk.missing_signals)
            assert risk.blacklisted is RiskSignal.UNKNOWN

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
