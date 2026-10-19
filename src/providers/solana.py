"""Solana JSON-RPC adapter for mint authorities and wallet balances."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import BaseUnits, PublicKey
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    EnhancedTokenSignals,
    Freshness,
    ProviderHealth,
    RiskSignal,
    TokenRiskSnapshot,
    WalletState,
)
from src.providers.contracts import (
    EnhancedTokenIntelligenceProvider,
    ProviderPayloadError,
    ProviderUnavailableError,
    RpcJsonClient,
)


class SolanaRpcProvider:
    def __init__(
        self,
        client: RpcJsonClient,
        *,
        clock: Clock | None = None,
        enhanced_provider: EnhancedTokenIntelligenceProvider | None = None,
    ) -> None:
        self._client = client
        self._clock = clock or SystemClock()
        self._enhanced = enhanced_provider

    @property
    def health(self) -> ProviderHealth:
        return self._client.health

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        now = self._clock.now()
        try:
            response = _response(
                await self._client.post_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [
                            mint.root,
                            {"encoding": "jsonParsed", "commitment": "confirmed"},
                        ],
                    }
                )
            )
        except ProviderUnavailableError:
            return _unavailable_token_risk(mint, now)
        result = _mapping(_required(response, "result"), "result")
        account = result.get("value")
        if account is None:
            raise ProviderPayloadError("Solana mint account does not exist")
        account_data = _mapping(_required(_mapping(account, "value"), "data"), "data")
        parsed = _mapping(_required(account_data, "parsed"), "parsed")
        info = _mapping(_required(parsed, "info"), "info")

        missing: list[str] = []
        mint_authority = _authority(info, "mintAuthority", missing)
        freeze_authority = _authority(info, "freezeAuthority", missing)
        enhanced = await self._enhanced_signals(mint, now)
        if enhanced.source_at > now:
            raise ProviderPayloadError("enhanced token source time is in the future")
        missing.extend(enhanced.missing_signals)
        deduplicated_missing = tuple(sorted(set(missing)))
        quality = DataQuality.PARTIAL if deduplicated_missing else DataQuality.COMPLETE
        return TokenRiskSnapshot(
            mint=mint,
            mint_authority=mint_authority,
            freeze_authority=freeze_authority,
            blacklisted=enhanced.blacklisted,
            verified=enhanced.verified,
            holder_concentration=enhanced.holder_concentration,
            wash_trading=enhanced.wash_trading,
            observed_at=now,
            source_at=enhanced.source_at,
            ingested_at=now,
            freshness=enhanced.freshness,
            data_quality=quality,
            missing_signals=deduplicated_missing,
            provider=(
                "solana_rpc" if self._enhanced is None else f"solana_rpc+{enhanced.provider}"
            ),
        )

    async def get_wallet_state(self, wallet: PublicKey) -> WalletState:
        now = self._clock.now()
        balance_response = _response(
            await self._client.post_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "getBalance",
                    "params": [wallet.root, {"commitment": "confirmed"}],
                }
            )
        )
        token_response = _response(
            await self._client.post_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet.root,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed", "commitment": "confirmed"},
                    ],
                }
            )
        )
        balance_result = _mapping(_required(balance_response, "result"), "result")
        native_balance = BaseUnits(_non_negative_int(_required(balance_result, "value"), "balance"))
        token_result = _mapping(_required(token_response, "result"), "result")
        raw_accounts = _required(token_result, "value")
        if not isinstance(raw_accounts, list):
            raise ProviderPayloadError("Solana token account result must be a list")
        balances: dict[str, int] = {}
        for raw_account in raw_accounts:
            account = _mapping(raw_account, "token account")
            account_value = _mapping(_required(account, "account"), "account")
            data = _mapping(_required(account_value, "data"), "data")
            parsed = _mapping(_required(data, "parsed"), "parsed")
            info = _mapping(_required(parsed, "info"), "info")
            mint = PublicKey(_required(info, "mint"))
            token_amount = _mapping(_required(info, "tokenAmount"), "tokenAmount")
            amount = _non_negative_int(_required(token_amount, "amount"), "token amount")
            balances[mint.root] = balances.get(mint.root, 0) + amount
        return WalletState(
            wallet=wallet,
            native_balance=native_balance,
            token_balances={mint: BaseUnits(amount) for mint, amount in balances.items()},
            observed_at=now,
            source_at=now,
            ingested_at=now,
            data_quality=DataQuality.COMPLETE,
        )

    async def _enhanced_signals(
        self, mint: PublicKey, observed_at: datetime
    ) -> EnhancedTokenSignals:
        if self._enhanced is None:
            return EnhancedTokenSignals(
                provider="none",
                source_at=observed_at,
                freshness=Freshness.STALE,
                missing_signals=(
                    "blacklisted",
                    "holder_concentration",
                    "verified",
                    "wash_trading",
                ),
            )
        try:
            return await self._enhanced.get_token_signals(mint)
        except ProviderUnavailableError:
            return EnhancedTokenSignals(
                provider="unavailable",
                source_at=observed_at,
                freshness=Freshness.STALE,
                missing_signals=(
                    "blacklisted",
                    "holder_concentration",
                    "verified",
                    "wash_trading",
                ),
            )


def _response(value: Any) -> Mapping[str, Any]:
    response = _mapping(value, "response")
    if "error" in response:
        raise ProviderUnavailableError("Solana RPC returned an error")
    return response


def _unavailable_token_risk(mint: PublicKey, now: datetime) -> TokenRiskSnapshot:
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
        data_quality=DataQuality.UNAVAILABLE,
        missing_signals=(
            "blacklisted",
            "freeze_authority",
            "holder_concentration",
            "mint_authority",
            "verified",
            "wash_trading",
        ),
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderPayloadError(f"Solana {field} must be an object")
    return value


def _required(payload: Mapping[str, Any], field: str) -> Any:
    if field not in payload:
        raise ProviderPayloadError(f"Solana payload is missing {field}")
    return payload[field]


def _authority(info: Mapping[str, Any], field: str, missing: list[str]) -> AuthorityStatus:
    if field not in info:
        missing.append(_snake_case(field))
        return AuthorityStatus.UNKNOWN
    value = info[field]
    if value is None:
        return AuthorityStatus.REVOKED
    if not isinstance(value, str):
        raise ProviderPayloadError(f"Solana {field} must be a public key or null")
    try:
        PublicKey(value)
    except ValueError as error:
        raise ProviderPayloadError(f"Solana {field} is not a public key") from error
    return AuthorityStatus.PRESENT


def _snake_case(value: str) -> str:
    output = ""
    for character in value:
        if character.isupper():
            output += "_"
        output += character.lower()
    return output


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProviderPayloadError(f"Solana {field} must be an integer")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProviderPayloadError(f"Solana {field} must be an integer") from error
    if parsed < 0:
        raise ProviderPayloadError(f"Solana {field} must not be negative")
    return parsed
