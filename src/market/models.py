"""Exact, provider-neutral market and token intelligence models."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from src.domain.models.primitives import BaseUnits, PublicKey

WINDOW_NAMES = frozenset({"5m", "30m", "1h", "4h", "24h"})
SHORT_WINDOW = "5m"
# Windows a complete snapshot must carry. The short 5m window is not one of them: the live
# Data API has none and it is derived from the cumulative counters of adjacent snapshots
# (D07), so its absence is a missing *signal* of the features, not incomplete market data.
LONG_WINDOW_NAMES = frozenset({"30m", "1h", "4h", "24h"})
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1
_U8_MAX = 255


class FrozenMapping[ValueT](Mapping[str, ValueT]):
    """A defensive, deepcopy-safe mapping for nested frozen model fields."""

    __slots__ = ("_values",)
    _values: Mapping[str, ValueT]

    def __init__(self, values: Mapping[str, ValueT]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> ValueT:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("frozen mapping is immutable")

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenMapping[ValueT]":
        del memo
        return self

    def as_dict(self) -> dict[str, ValueT]:
        return dict(self._values)


def _exact_decimal(value: Any) -> Any:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float is forbidden for monetary values")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class DataQuality(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"


class ProviderState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    state: ProviderState
    consecutive_failures: int
    last_error: str | None
    open_until: float | None


class AuthorityStatus(StrEnum):
    REVOKED = "REVOKED"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class RiskSignal(StrEnum):
    SAFE = "SAFE"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class PoolRef(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    address: PublicKey
    base_mint: PublicKey
    quote_mint: PublicKey


class PoolPage(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    items: tuple[PoolRef, ...]
    next_cursor: str | None = None


class WindowMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    volume: Decimal
    fees: Decimal | None = None
    transaction_count: int | None = None
    unique_participants: int | None = None
    # The protocol's share of the swap fees; the live Data API reports it apart from
    # ``fees`` (the LP share), so the whole fee paid is ``fees + protocol_fees``.
    protocol_fees: Decimal | None = None

    _reject_binary_float = field_validator("volume", "fees", "protocol_fees", mode="before")(
        _exact_decimal
    )

    @model_validator(mode="after")
    def validate_non_negative(self) -> "WindowMetrics":
        if (
            self.volume < 0
            or (self.fees is not None and self.fees < 0)
            or (self.protocol_fees is not None and self.protocol_fees < 0)
        ):
            raise ValueError("window monetary values must not be negative")
        if self.transaction_count is not None and self.transaction_count < 0:
            raise ValueError("transaction_count must not be negative")
        if self.unique_participants is not None and self.unique_participants < 0:
            raise ValueError("unique_participants must not be negative")
        return self

    @property
    def total_fees(self) -> Decimal | None:
        """Every fee the window's swaps paid: LP ``fees`` plus ``protocol_fees`` if reported."""

        if self.fees is None:
            return None
        return self.fees + (self.protocol_fees or Decimal(0))


class WindowOrigin(StrEnum):
    """Where a short-window figure came from; stored with the snapshot and the decision."""

    REPORTED = "REPORTED"
    CUMULATIVE_DELTA = "CUMULATIVE_DELTA"
    VOLUME_HISTORY = "VOLUME_HISTORY"


class ShortWindowStatus(StrEnum):
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class CounterMark(BaseModel):
    """The highest reading of one lifetime counter, when it was seen, and the run of
    lower readings since (``low_since``/``low_last``) that may turn out to be a reset."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    value: Decimal
    at: datetime
    low_since: datetime | None = None
    low_last: Decimal | None = None

    _reject_binary_float = field_validator("value", "low_last", mode="before")(_exact_decimal)
    _normalize_at = field_validator("at")(_utc)

    @field_validator("low_since")
    @classmethod
    def _normalize_low_since(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_mark(self) -> "CounterMark":
        if (self.low_since is None) != (self.low_last is None):
            raise ValueError("a run of low readings needs both its start and its last value")
        if self.low_last is not None and self.low_last >= self.value:
            raise ValueError("a low reading must be below the mark")
        return self


class ShortWindowDerivation(BaseModel):
    """How the short window of one snapshot was derived from the previous snapshot (D07).

    ``DERIVED``: the window holds the counters' increase over ``interval_seconds``,
    rescaled to the window length. ``UNKNOWN``: the snapshot carries no such window and
    ``reason`` says why (``NO_PREVIOUS``, ``COUNTER_MISSING``, ``INTERVAL_TOO_SHORT``,
    ``INTERVAL_TOO_LONG``, ``NEGATIVE_DELTA``, ``COUNTER_RESET``).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    window: str
    origin: WindowOrigin
    status: ShortWindowStatus
    reason: str | None = None
    previous_source_at: datetime | None = None
    interval_seconds: Decimal | None = None
    volume_delta: Decimal | None = None
    fees_delta: Decimal | None = None
    zeroed_noise: bool = False
    # The highest counter readings up to this snapshot and when they were seen: the next
    # increase is measured from them over its own interval, so a counter that dropped never
    # becomes the base. ``None`` while a counter is unknown.
    volume_mark: CounterMark | None = None
    fees_mark: CounterMark | None = None

    _reject_binary_float = field_validator(
        "interval_seconds", "volume_delta", "fees_delta", mode="before"
    )(_exact_decimal)

    @field_validator("previous_source_at")
    @classmethod
    def _normalize_previous(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_derivation(self) -> "ShortWindowDerivation":
        if self.window not in WINDOW_NAMES:
            raise ValueError("derived window must be a known metric window")
        if (self.status is ShortWindowStatus.UNKNOWN) == (self.reason is None):
            raise ValueError("an unknown short window needs a reason, a derived one none")
        if self.status is ShortWindowStatus.DERIVED and (
            self.interval_seconds is None or self.interval_seconds <= 0
        ):
            raise ValueError("a derived short window needs its positive interval")
        return self


class VolumeBucket(BaseModel):
    """One closed bucket of a pool's volume history (``/volume/history``, D07 warm-up)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    pool_address: PublicKey
    start: datetime
    duration: timedelta
    volume: Decimal
    fees: Decimal | None = None
    protocol_fees: Decimal | None = None

    _reject_binary_float = field_validator("volume", "fees", "protocol_fees", mode="before")(
        _exact_decimal
    )
    _normalize_utc = field_validator("start")(_utc)

    @model_validator(mode="after")
    def validate_bucket(self) -> "VolumeBucket":
        if self.duration <= timedelta(0):
            raise ValueError("bucket duration must be positive")
        if any(
            value is not None and value < 0
            for value in (self.volume, self.fees, self.protocol_fees)
        ):
            raise ValueError("bucket values must not be negative")
        return self

    @property
    def end(self) -> datetime:
        return self.start + self.duration


class PoolSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    pool: PoolRef
    windows: Mapping[str, WindowMetrics]
    tvl: Decimal
    depth: Decimal | None
    price: Decimal
    bin_step: int
    created_at: datetime
    observed_at: datetime
    source_at: datetime
    ingested_at: datetime
    freshness: Freshness
    data_quality: DataQuality
    missing_fields: tuple[str, ...] = ()
    provider: str = "unknown"
    # Optional guide signals (spec 6.6). ``None`` means the source did not report them;
    # they are not part of ``missing_fields`` so rulesets that do not use them are unaffected.
    base_fee_rate: Decimal | None = None
    base_token_market_cap: Decimal | None = None
    # Execution anchor (spec 8.5, D06): the pool's active bin and both token decimals.
    # ``None`` when no source reported them; a non-SHADOW entry needs all three.
    active_bin_id: int | None = None
    base_decimals: int | None = None
    quote_decimals: int | None = None
    # The window standing in for the short 5m window when the source has none (D04),
    # e.g. ``"30m"`` from the live Data API; ``None`` when the source made no substitution.
    short_window_source: str | None = None
    # Lifetime volume and fee counters (``cumulative_metrics``, D07): the short window is
    # their increase since the previous snapshot. ``None`` when the source has none.
    cumulative_volume: Decimal | None = None
    cumulative_fees: Decimal | None = None
    # How the short window was derived from the previous snapshot; ``None`` when the
    # source reported it or no derivation was attempted.
    short_window: ShortWindowDerivation | None = None
    # Hard-filter signals the Data API reports with the pool (G06, D07); ``None`` = unknown.
    pool_blacklisted: bool | None = None
    base_token_verified: bool | None = None
    quote_token_verified: bool | None = None
    base_freeze_authority_disabled: bool | None = None

    _reject_binary_float = field_validator(
        "tvl",
        "depth",
        "price",
        "base_fee_rate",
        "base_token_market_cap",
        "cumulative_volume",
        "cumulative_fees",
        mode="before",
    )(_exact_decimal)
    _normalize_utc = field_validator("created_at", "observed_at", "source_at", "ingested_at")(_utc)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PoolSnapshot":
        if self.tvl < 0 or (self.depth is not None and self.depth < 0) or self.price < 0:
            raise ValueError("snapshot monetary values must not be negative")
        if any(
            value is not None and value < 0
            for value in (self.cumulative_volume, self.cumulative_fees)
        ):
            raise ValueError("cumulative counters must not be negative")
        derivation = self.short_window
        if derivation is not None and (
            (derivation.status is ShortWindowStatus.DERIVED)
            != (derivation.window in self.windows)
        ):
            raise ValueError("the snapshot must carry exactly the windows its derivation made")
        if self.base_fee_rate is not None and not Decimal(0) <= self.base_fee_rate <= Decimal(1):
            raise ValueError("base_fee_rate must be a fraction in [0, 1]")
        if self.base_token_market_cap is not None and self.base_token_market_cap < 0:
            raise ValueError("base_token_market_cap must not be negative")
        if self.bin_step <= 0:
            raise ValueError("bin_step must be positive")
        if self.active_bin_id is not None and not _I32_MIN <= self.active_bin_id <= _I32_MAX:
            raise ValueError("active_bin_id must fit the on-chain i32")
        for decimals in (self.base_decimals, self.quote_decimals):
            if decimals is not None and not 0 <= decimals <= _U8_MAX:
                raise ValueError("token decimals must fit the on-chain u8")
        if self.short_window_source is not None and self.short_window_source not in self.windows:
            raise ValueError("short_window_source must name a window the snapshot carries")
        unknown_windows = set(self.windows).difference(WINDOW_NAMES)
        if unknown_windows:
            raise ValueError(f"unknown metric windows: {sorted(unknown_windows)}")
        if self.data_quality is DataQuality.COMPLETE and not LONG_WINDOW_NAMES.issubset(
            self.windows
        ):
            raise ValueError("complete snapshot must contain every long metric window")
        if self.data_quality is DataQuality.COMPLETE and self.missing_fields:
            raise ValueError("complete snapshot cannot contain missing fields")
        if self.source_at > self.observed_at or self.observed_at > self.ingested_at:
            raise ValueError("snapshot timestamps must be source <= observed <= ingested")
        object.__setattr__(self, "windows", FrozenMapping(self.windows))
        return self

    @field_serializer("windows")
    def serialize_windows(self, value: Mapping[str, WindowMetrics]) -> dict[str, WindowMetrics]:
        return dict(value)

    @property
    def pool_age(self) -> timedelta:
        return self.observed_at - self.created_at


class TokenRiskSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    mint: PublicKey
    mint_authority: AuthorityStatus
    freeze_authority: AuthorityStatus
    blacklisted: RiskSignal
    verified: RiskSignal
    holder_concentration: Decimal | None
    observed_at: datetime
    source_at: datetime
    ingested_at: datetime
    freshness: Freshness = Freshness.FRESH
    data_quality: DataQuality
    missing_signals: tuple[str, ...] = ()
    provider: str = "solana_rpc"
    wash_trading: RiskSignal = RiskSignal.UNKNOWN
    # Optional guide signals (spec 6.6), ``None`` when no source reported them. The oldest
    # mint activity seen is an exact creation time only when ``activity_history_complete``;
    # otherwise it is a lower bound on the token's age.
    earliest_activity_at: datetime | None = None
    activity_history_complete: bool = False
    total_fees_sol: Decimal | None = None

    _reject_binary_float = field_validator("holder_concentration", "total_fees_sol", mode="before")(
        _exact_decimal
    )
    _normalize_utc = field_validator("observed_at", "source_at", "ingested_at")(_utc)

    @field_validator("earliest_activity_at")
    @classmethod
    def _normalize_activity(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_risk(self) -> "TokenRiskSnapshot":
        if self.holder_concentration is not None and not (
            Decimal(0) <= self.holder_concentration <= Decimal(1)
        ):
            raise ValueError("holder_concentration must be between zero and one")
        if self.total_fees_sol is not None and self.total_fees_sol < 0:
            raise ValueError("total_fees_sol must not be negative")
        if self.activity_history_complete and self.earliest_activity_at is None:
            raise ValueError("complete activity history requires earliest_activity_at")
        if self.earliest_activity_at is not None and self.earliest_activity_at > self.observed_at:
            raise ValueError("earliest_activity_at cannot be after observed_at")
        unknown_signals: set[str] = set()
        if self.mint_authority is AuthorityStatus.UNKNOWN:
            unknown_signals.add("mint_authority")
        if self.freeze_authority is AuthorityStatus.UNKNOWN:
            unknown_signals.add("freeze_authority")
        if self.blacklisted is RiskSignal.UNKNOWN:
            unknown_signals.add("blacklisted")
        if self.verified is RiskSignal.UNKNOWN:
            unknown_signals.add("verified")
        if self.wash_trading is RiskSignal.UNKNOWN:
            unknown_signals.add("wash_trading")
        if self.holder_concentration is None:
            unknown_signals.add("holder_concentration")
        if not unknown_signals.issubset(self.missing_signals):
            raise ValueError("unknown risk signals must be listed in missing_signals")
        if self.data_quality is DataQuality.COMPLETE and self.missing_signals:
            raise ValueError("complete token risk snapshot cannot have missing signals")
        if self.source_at > self.observed_at or self.observed_at > self.ingested_at:
            raise ValueError("risk timestamps must be source <= observed <= ingested")
        return self


class EnhancedTokenSignals(BaseModel):
    """Optional signals that are not available from vanilla Solana RPC."""

    model_config = ConfigDict(frozen=True, strict=True)

    blacklisted: RiskSignal = RiskSignal.UNKNOWN
    verified: RiskSignal = RiskSignal.UNKNOWN
    wash_trading: RiskSignal = RiskSignal.UNKNOWN
    holder_concentration: Decimal | None = None
    total_fees_sol: Decimal | None = None
    missing_signals: tuple[str, ...] = ()
    provider: str
    source_at: datetime
    freshness: Freshness

    _reject_binary_float = field_validator("holder_concentration", "total_fees_sol", mode="before")(
        _exact_decimal
    )
    _normalize_utc = field_validator("source_at")(_utc)


class WalletState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    wallet: PublicKey
    native_balance: BaseUnits
    token_balances: Mapping[str, BaseUnits]
    observed_at: datetime
    source_at: datetime
    ingested_at: datetime
    data_quality: DataQuality
    provider: str = "solana_rpc"

    _normalize_utc = field_validator("observed_at", "source_at", "ingested_at")(_utc)

    @model_validator(mode="after")
    def freeze_balances(self) -> "WalletState":
        object.__setattr__(self, "token_balances", FrozenMapping(self.token_balances))
        return self

    @field_serializer("token_balances")
    def serialize_balances(self, value: Mapping[str, BaseUnits]) -> dict[str, BaseUnits]:
        return dict(value)
