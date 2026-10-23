"""Solana JSON-RPC adapter: mint authorities, holder concentration, mint age, wallets and
the DLMM execution anchor (active bin, token decimals)."""

import base64
import binascii
import hashlib
import struct
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import BaseUnits, PublicKey
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    EnhancedTokenSignals,
    Freshness,
    PoolRef,
    ProviderHealth,
    RiskSignal,
    TokenRiskSnapshot,
    WalletState,
)
from src.providers.contracts import (
    EnhancedTokenIntelligenceProvider,
    LbPairState,
    ProviderError,
    ProviderPayloadError,
    ProviderUnavailableError,
    RpcJsonClient,
)

# Programs whose accounts hold liquidity or launch inventory, not a holder's free float:
# a token account whose authority is owned by one of them is a pool vault or curve.
KNOWN_LIQUIDITY_PROGRAMS: frozenset[str] = frozenset(
    {
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
        "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DAMM v1
        "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
        "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora Dynamic Bonding Curve
        "24Uqj9JCLxUeoC3hGfh5W3s9FM9uCHDS2SG3LYwBpyTi",  # Meteora Vault
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    }
)
# Program-derived authorities that are not owned by their program (system-owned PDAs).
KNOWN_PROGRAM_AUTHORITIES: frozenset[str] = frozenset(
    {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"}  # Raydium AMM v4 authority
)
DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
# Prefix of the DLMM ``LbPair`` account (Anchor zero-copy, little-endian): discriminator (8),
# StaticParameters (32), VariableParameters (32), bump_seed (1), bin_step_seed (2),
# pair_type (1), active_id i32 at 76, bin_step u16 at 80, status, require_base_factor_seed,
# base_factor_seed (2), activation_type, creator_pool_on_off_control, token_x_mint at 88,
# token_y_mint at 120. Checked on mainnet on 2026-10-23: for three pools the mints, bin step
# and the price implied by active_id matched the Data API. Anything else is not read.
_LB_PAIR_DISCRIMINATOR = hashlib.sha256(b"account:LbPair").digest()[:8]
_LB_PAIR_PREFIX_LENGTH = 152
_ACTIVE_ID_OFFSET = 76
_BIN_STEP_OFFSET = 80
_TOKEN_X_MINT_OFFSET = 88
_TOKEN_Y_MINT_OFFSET = 120
_ACCOUNTS_PER_CALL = 100
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TOP_HOLDERS = 10
_SIGNATURE_PAGE = 1_000
_MAX_CACHED_MINTS = 4_096
# Signals vanilla RPC cannot give: unknown (and missing) without an enhanced reading.
_ENHANCED_ONLY_SIGNALS = ("blacklisted", "holder_concentration", "verified", "wash_trading")


@dataclass(frozen=True, slots=True)
class _Activity:
    earliest_at: datetime
    complete: bool


class SolanaRpcProvider:
    def __init__(
        self,
        client: RpcJsonClient,
        *,
        clock: Clock | None = None,
        enhanced_provider: EnhancedTokenIntelligenceProvider | None = None,
        analyze_holders: bool = True,
        activity_pages: int = 1,
        liquidity_programs: Collection[str] = KNOWN_LIQUIDITY_PROGRAMS,
        program_authorities: Collection[str] = KNOWN_PROGRAM_AUTHORITIES,
    ) -> None:
        if activity_pages < 0:
            raise ValueError("activity_pages cannot be negative")
        self._client = client
        self._clock = clock or SystemClock()
        self._enhanced = enhanced_provider
        self._analyze_holders = analyze_holders
        self._activity_pages = activity_pages
        self._liquidity_programs = frozenset(liquidity_programs)
        self._program_authorities = frozenset(program_authorities)
        self._complete_activity: dict[str, _Activity] = {}

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
        received = await self._enhanced_signals(mint)
        if received is not None and received.source_at > now:
            raise ProviderPayloadError("enhanced token source time is in the future")
        # D09: the token is as fresh as the data it carries. The RPC read is from now; an
        # enhanced reading counts only when one was received (with its own age), never its
        # absence, whose signals stay unknown and are listed as missing.
        enhanced = received or EnhancedTokenSignals(
            provider="unavailable",
            source_at=now,
            freshness=Freshness.FRESH,
            missing_signals=_ENHANCED_ONLY_SIGNALS,
        )
        missing.extend(enhanced.missing_signals)
        # The more concentrated reading wins: a risk filter must never under-report it.
        readings = [
            value
            for value in (await self._top_holder_share(mint), enhanced.holder_concentration)
            if value is not None
        ]
        holder_concentration = max(readings) if readings else None
        if holder_concentration is None:
            missing.append("holder_concentration")
        else:
            missing = [name for name in missing if name != "holder_concentration"]
        activity = await self._activity(mint, now)
        deduplicated_missing = tuple(sorted(set(missing)))
        quality = DataQuality.PARTIAL if deduplicated_missing else DataQuality.COMPLETE
        return TokenRiskSnapshot(
            mint=mint,
            mint_authority=mint_authority,
            freeze_authority=freeze_authority,
            blacklisted=enhanced.blacklisted,
            verified=enhanced.verified,
            holder_concentration=holder_concentration,
            earliest_activity_at=None if activity is None else activity.earliest_at,
            activity_history_complete=activity is not None and activity.complete,
            total_fees_sol=enhanced.total_fees_sol,
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

    async def get_pool_states(self, pools: Sequence[PoolRef]) -> dict[str, LbPairState]:
        """Active bin, bin step and mints of each DLMM pair; non-pair accounts are left out."""

        addresses = [pool.address.root for pool in pools]
        states: dict[str, LbPairState] = {}
        options = {
            "encoding": "base64",
            "commitment": "confirmed",
            "dataSlice": {"offset": 0, "length": _LB_PAIR_PREFIX_LENGTH},
        }
        for offset in range(0, len(addresses), _ACCOUNTS_PER_CALL):
            chunk = addresses[offset : offset + _ACCOUNTS_PER_CALL]
            values = _accounts(await self._call("getMultipleAccounts", [chunk, options]), chunk)
            for address, raw in zip(chunk, values, strict=True):
                state = _lb_pair_state(raw)
                if state is not None:
                    states[address] = state
        return states

    async def get_mint_decimals(self, mints: Sequence[PublicKey]) -> dict[str, int]:
        """Decimals of parsed SPL mint accounts; any other account is left out."""

        addresses = [mint.root for mint in mints]
        decimals: dict[str, int] = {}
        options = {"encoding": "jsonParsed", "commitment": "confirmed"}
        for offset in range(0, len(addresses), _ACCOUNTS_PER_CALL):
            chunk = addresses[offset : offset + _ACCOUNTS_PER_CALL]
            values = _accounts(await self._call("getMultipleAccounts", [chunk, options]), chunk)
            for address, raw in zip(chunk, values, strict=True):
                value = _mint_decimals(raw)
                if value is not None:
                    decimals[address] = value
        return decimals

    async def _enhanced_signals(self, mint: PublicKey) -> EnhancedTokenSignals | None:
        """The enhanced provider's reading, or ``None`` when there is none or it is down."""

        if self._enhanced is None:
            return None
        try:
            return await self._enhanced.get_token_signals(mint)
        except ProviderUnavailableError:
            return None

    async def _top_holder_share(self, mint: PublicKey) -> Decimal | None:
        """Top-10 wallet share of supply, excluding pool vaults and known programs.

        ``getTokenLargestAccounts`` shows at most one page of token accounts; when that page
        is full and fewer than ten free-float holders remain after exclusions, the true
        top-10 may include unseen accounts, so the signal stays unknown.
        """

        if not self._analyze_holders:
            return None
        try:
            supply = _supply(await self._call("getTokenSupply", [mint.root]))
            largest = _largest_accounts(await self._call("getTokenLargestAccounts", [mint.root]))
            if supply <= 0 or not largest:
                return None
            authorities = _authorities(
                await self._call(
                    "getMultipleAccounts",
                    [[address for address, _ in largest], {"encoding": "jsonParsed"}],
                ),
                len(largest),
            )
            unique = sorted(set(authorities))
            owners = _owner_programs(
                await self._call(
                    "getMultipleAccounts",
                    [unique, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}],
                ),
                unique,
            )
        except ProviderError:
            return None
        holdings: dict[str, int] = {}
        for (_, amount), authority in zip(largest, authorities, strict=True):
            program, executable = owners[authority]
            if (
                executable
                or authority in self._program_authorities
                or authority in self._liquidity_programs
                or program in self._liquidity_programs
            ):
                continue
            holdings[authority] = holdings.get(authority, 0) + amount
        page_is_full = len(largest) >= 20
        if page_is_full and len(holdings) < _TOP_HOLDERS:
            return None
        top = sorted(holdings.values(), reverse=True)[:_TOP_HOLDERS]
        return min(Decimal(sum(top)) / Decimal(supply), Decimal(1))

    async def _activity(self, mint: PublicKey, now: datetime) -> _Activity | None:
        """Oldest mint activity: an exact creation time once the history ends, else a bound."""

        cached = self._complete_activity.get(mint.root)
        if cached is not None:
            return cached
        if self._activity_pages == 0:
            return None
        earliest: datetime | None = None
        before: str | None = None
        complete = False
        try:
            for _ in range(self._activity_pages):
                options: dict[str, Any] = {"limit": _SIGNATURE_PAGE, "commitment": "confirmed"}
                if before is not None:
                    options["before"] = before
                page = _signature_page(
                    await self._call("getSignaturesForAddress", [mint.root, options])
                )
                for entry in page:
                    block_time = entry.get("blockTime")
                    if block_time is None:
                        continue
                    seen = _block_time(block_time)
                    earliest = seen if earliest is None else min(earliest, seen)
                if len(page) < _SIGNATURE_PAGE:
                    complete = True
                    break
                signature = page[-1].get("signature")
                if not isinstance(signature, str):
                    break
                before = signature
        except ProviderError:
            return None
        if earliest is None:
            return None
        activity = _Activity(earliest_at=min(earliest, now), complete=complete)
        if complete:
            if len(self._complete_activity) >= _MAX_CACHED_MINTS:
                self._complete_activity.pop(next(iter(self._complete_activity)))
            self._complete_activity[mint.root] = activity
        return activity

    async def _call(self, method: str, params: list[Any]) -> Mapping[str, Any]:
        return _response(
            await self._client.post_json(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
        )


def _response(value: Any) -> Mapping[str, Any]:
    response = _mapping(value, "response")
    if "error" in response:
        raise ProviderUnavailableError("Solana RPC returned an error")
    return response


def _rpc_value(response: Mapping[str, Any]) -> Any:
    return _required(_mapping(_required(response, "result"), "result"), "value")


def _rpc_list(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ProviderPayloadError(f"Solana {field} must be a list")
    return value


def _account_key(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProviderPayloadError(f"Solana {field} must be a public key")
    try:
        return PublicKey(value).root
    except ValueError as error:
        raise ProviderPayloadError(f"Solana {field} is not a public key") from error


def _accounts(response: Mapping[str, Any], addresses: Sequence[str]) -> Sequence[Any]:
    values = _rpc_list(_rpc_value(response), "accounts")
    if len(values) != len(addresses):
        raise ProviderPayloadError("Solana returned a different number of accounts")
    return values


def _lb_pair_state(raw: Any) -> LbPairState | None:
    if raw is None:
        return None
    account = _mapping(raw, "LB pair account")
    if account.get("owner") != DLMM_PROGRAM_ID:
        return None
    data = account.get("data")
    if (
        not isinstance(data, list)
        or len(data) != 2
        or not isinstance(data[0], str)
        or data[1] != "base64"
    ):
        raise ProviderPayloadError("Solana account data must be base64")
    try:
        prefix = base64.b64decode(data[0], validate=True)
    except binascii.Error as error:
        raise ProviderPayloadError("Solana account data is not base64") from error
    if len(prefix) != _LB_PAIR_PREFIX_LENGTH or prefix[:8] != _LB_PAIR_DISCRIMINATOR:
        return None  # another DLMM account type (position, bin array, ...)
    (active_bin_id,) = struct.unpack_from("<i", prefix, _ACTIVE_ID_OFFSET)
    (bin_step,) = struct.unpack_from("<H", prefix, _BIN_STEP_OFFSET)
    return LbPairState(
        active_bin_id=active_bin_id,
        bin_step=bin_step,
        token_x_mint=_base58(prefix[_TOKEN_X_MINT_OFFSET : _TOKEN_X_MINT_OFFSET + 32]),
        token_y_mint=_base58(prefix[_TOKEN_Y_MINT_OFFSET : _TOKEN_Y_MINT_OFFSET + 32]),
    )


def _mint_decimals(raw: Any) -> int | None:
    if raw is None:
        return None
    data = _mapping(raw, "mint account").get("data")
    if not isinstance(data, Mapping):
        return None  # not parsed by the RPC: not a token-program account
    parsed = data.get("parsed")
    if not isinstance(parsed, Mapping) or parsed.get("type") != "mint":
        return None
    info = parsed.get("info")
    value = info.get("decimals") if isinstance(info, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ProviderPayloadError("Solana mint decimals must be an integer in [0, 255]")
    return value


def _base58(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + encoded


def _supply(response: Mapping[str, Any]) -> int:
    value = _mapping(_rpc_value(response), "token supply")
    return _non_negative_int(_required(value, "amount"), "token supply")


def _largest_accounts(response: Mapping[str, Any]) -> list[tuple[str, int]]:
    accounts: list[tuple[str, int]] = []
    for raw in _rpc_list(_rpc_value(response), "largest accounts"):
        entry = _mapping(raw, "largest account")
        accounts.append(
            (
                _account_key(_required(entry, "address"), "token account"),
                _non_negative_int(_required(entry, "amount"), "token account amount"),
            )
        )
    return accounts


def _authorities(response: Mapping[str, Any], expected: int) -> list[str]:
    values = _rpc_list(_rpc_value(response), "token accounts")
    if len(values) != expected:
        raise ProviderPayloadError("Solana returned a different number of token accounts")
    authorities: list[str] = []
    for raw in values:
        account = _mapping(raw, "token account")
        data = _mapping(_required(account, "data"), "data")
        info = _mapping(_required(_mapping(_required(data, "parsed"), "parsed"), "info"), "info")
        authorities.append(_account_key(_required(info, "owner"), "token account owner"))
    return authorities


def _owner_programs(
    response: Mapping[str, Any], authorities: Sequence[str]
) -> dict[str, tuple[str | None, bool]]:
    values = _rpc_list(_rpc_value(response), "authority accounts")
    if len(values) != len(authorities):
        raise ProviderPayloadError("Solana returned a different number of authority accounts")
    owners: dict[str, tuple[str | None, bool]] = {}
    for authority, raw in zip(authorities, values, strict=True):
        if raw is None:  # an unfunded wallet has no account at all
            owners[authority] = (None, False)
            continue
        account = _mapping(raw, "authority account")
        executable = account.get("executable", False)
        if not isinstance(executable, bool):
            raise ProviderPayloadError("Solana executable flag must be a boolean")
        owners[authority] = (_account_key(_required(account, "owner"), "account owner"), executable)
    return owners


def _signature_page(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        _mapping(entry, "signature")
        for entry in _rpc_list(_required(response, "result"), "signatures")
    ]


def _block_time(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderPayloadError("Solana blockTime must be a non-negative integer")
    return datetime.fromtimestamp(value, tz=UTC)


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
