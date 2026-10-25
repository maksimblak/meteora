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


class CounterReading(BaseModel):
    """One reading of a lifetime counter and when it was seen."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    value: Decimal
    at: datetime

    _reject_binary_float = field_validator("value", mode="before")(_exact_decimal)
    _normalize_at = field_validator("at")(_utc)


class CounterMark(BaseModel):
    """The highest reading of one lifetime counter, when it was seen, and the run of
    lower readings since (``low_since``/``low_last``) that may turn out to be a reset.

    ``earlier``: older highs, oldest first, still close enough to start the next window -
    a window spans at least the window length less its tolerance, so readings that came
    sooner than that are summed until they cover it, never stretched (A1). ``reset_from``:
    the highest reading before the last counter reset; a counter that climbs back to it
    is the old counter going on, not growth since the reset (A2).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    value: Decimal
    at: datetime
    low_since: datetime | None = None
    low_last: Decimal | None = None
    earlier: tuple[CounterReading, ...] = ()
    reset_from: CounterReading | None = None

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
        previous: CounterReading | None = None
        for reading in (*self.earlier, CounterReading(value=self.value, at=self.at)):
            if previous is not None and (
                reading.at <= previous.at or reading.value < previous.value
            ):
                raise ValueError("earlier highs must precede the mark and never exceed it")
            previous = reading
        if self.reset_from is not None and self.reset_from.value <= self.value:
            raise ValueError("the reading before a reset must be above the counter after it")
        return self


class ShortWindowDerivation(BaseModel):
    """How the short window of one snapshot was derived from the previous snapshot (D07).

    ``DERIVED``: the window holds the counters' increase over ``interval_seconds`` (never
    shorter than the window less its tolerance), rescaled to the window length.
    ``UNKNOWN``: the snapshot carries no such window and
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
    # USD price of one whole quote token (Data API ``token_y.price``, by mint). With
    # ``quote_decimals`` it turns the USD reference size and USD depth into quote-token base
    # units (A3); ``None`` (or zero) when the source reports no price.
    quote_price_usd: Decimal | None = None
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
    # The base token's name and ticker as the source reports them (theme filter, G24);
    # ``None`` when the source has none.
    base_token_name: str | None = None
    base_token_symbol: str | None = None

    _reject_binary_float = field_validator(
        "tvl",
        "depth",
        "price",
        "base_fee_rate",
        "base_token_market_cap",
        "quote_price_usd",
        "cumulative_volume",
        "cumulative_fees",
        mode="before",
    )(_exact_decimal)
    _normalize_utc = field_validator("created_at", "observed_at", "source_at", "ingested_at")(_utc)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PoolSnapshot":
        if self.tvl < 0 or (self.depth is not None and self.depth < 0) or self.price < 0:
            raise ValueError("snapshot monetary values must not be negative")
        if self.quote_price_usd is not None and self.quote_price_usd < 0:
            raise ValueError("quote_price_usd must not be negative")
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


_SHARE_FIELDS = (
    "top10_holder_share",
    "insiders_share",
    "bundling_share",
    "phishing_share",
    "developer_share",
    "sniper_share",
)
# Token-level USD figures of an enhanced source (GMGN, T18); never negative.
_USD_FIELDS = (
    "volume_5m_usd",
    "volume_1h_usd",
    "price_usd",
    "market_cap_usd",
    "ath_price_usd",
    "ath_market_cap_usd",
)


class DeveloperStatus(StrEnum):
    """What the token's creator did with the supply (GMGN ``creator_token_status``)."""

    HOLDING = "HOLDING"
    SOLD = "SOLD"


class EnhancedTokenSignals(BaseModel):
    """Optional signals that are not available from vanilla Solana RPC.

    ``holder_concentration`` is a top-10 share in the RPC sense (pool vaults and programs
    excluded) that may replace an unknown RPC reading. The holder-structure shares of a
    token page (GMGN, G18) are fractions in [0, 1] as the source reports them: its own
    ``top10_holder_share``, ``insiders_share``, ``bundling_share``, ``phishing_share`` and
    the developer's ``developer_share``. ``None`` = the source did not report it;
    ``detail`` says why a reading is unavailable.

    Token-level market figures (T18, G24), all USD and over every pool of the token as the
    author reads them in GMGN: ``volume_5m_usd`` and ``volume_1h_usd``, ``price_usd`` and
    ``market_cap_usd``, the all-time high ``ath_price_usd`` and ``ath_market_cap_usd``.
    ``sniper_count`` wallets bought at launch and hold ``sniper_share``;
    ``developer_status`` says whether the creator still holds or sold; ``created_at`` is the
    token's creation time.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    blacklisted: RiskSignal = RiskSignal.UNKNOWN
    verified: RiskSignal = RiskSignal.UNKNOWN
    wash_trading: RiskSignal = RiskSignal.UNKNOWN
    holder_concentration: Decimal | None = None
    total_fees_sol: Decimal | None = None
    top10_holder_share: Decimal | None = None
    insiders_share: Decimal | None = None
    bundling_share: Decimal | None = None
    phishing_share: Decimal | None = None
    developer_share: Decimal | None = None
    missing_signals: tuple[str, ...] = ()
    provider: str
    source_at: datetime
    freshness: Freshness
    detail: str | None = None
    volume_5m_usd: Decimal | None = None
    volume_1h_usd: Decimal | None = None
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    ath_price_usd: Decimal | None = None
    ath_market_cap_usd: Decimal | None = None
    sniper_count: int | None = None
    sniper_share: Decimal | None = None
    developer_status: DeveloperStatus | None = None
    created_at: datetime | None = None

    _reject_binary_float = field_validator(
        "holder_concentration", "total_fees_sol", *_SHARE_FIELDS, *_USD_FIELDS, mode="before"
    )(_exact_decimal)
    _normalize_utc = field_validator("source_at")(_utc)

    @field_validator("created_at")
    @classmethod
    def _normalize_created(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_signals(self) -> "EnhancedTokenSignals":
        for name in ("holder_concentration", *_SHARE_FIELDS):
            value = getattr(self, name)
            if value is not None and not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name} must be a fraction in [0, 1]")
        if self.total_fees_sol is not None and self.total_fees_sol < 0:
            raise ValueError("total_fees_sol must not be negative")
        for name in _USD_FIELDS:
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.sniper_count is not None and self.sniper_count < 0:
            raise ValueError("sniper_count must not be negative")
        return self


class OrganicScoreLabel(StrEnum):
    """Jupiter's own grade of a token's ``organicScore`` (Tokens API v2)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Token-level USD figures of a Jupiter reading (T22); never negative.
_JUPITER_USD_FIELDS = (
    "volume_5m_usd",
    "volume_1h_usd",
    "price_usd",
    "market_cap_usd",
    "liquidity_usd",
)


class JupiterTokenReading(BaseModel):
    """One token as Jupiter Tokens API v2 (``/tokens/v2/search``) reports it (T22).

    Token-level figures over every venue Jupiter routes, as the author reads a token in
    GMGN: ``volume_5m_usd`` and ``volume_1h_usd`` (buy plus sell volume of the window),
    ``price_usd``, ``market_cap_usd`` and ``liquidity_usd``. ``trades_5m`` (buys plus sells)
    and ``traders_5m`` are the 5-minute window's transaction and wallet counts.
    ``organic_score`` (0..100) is Jupiter's measure of organic activity, graded
    ``organic_score_label``. ``mint_authority`` and ``freeze_authority``: REVOKED when
    Jupiter reports the authority disabled, PRESENT when it names one. ``top_holders_share``
    and ``developer_share`` are fractions in [0, 1]; ``first_pool_at`` is the creation of
    the token's first pool Jupiter knows - a lower bound on the token's age, not its mint
    time. ``None`` = not reported; ``detail`` says why a reading is unavailable.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    mint: PublicKey
    provider: str = "jupiter"
    source_at: datetime
    freshness: Freshness
    detail: str | None = None
    volume_5m_usd: Decimal | None = None
    volume_1h_usd: Decimal | None = None
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    trades_5m: int | None = None
    traders_5m: int | None = None
    organic_score: Decimal | None = None
    organic_score_label: OrganicScoreLabel | None = None
    mint_authority: AuthorityStatus = AuthorityStatus.UNKNOWN
    freeze_authority: AuthorityStatus = AuthorityStatus.UNKNOWN
    top_holders_share: Decimal | None = None
    developer_share: Decimal | None = None
    holder_count: int | None = None
    first_pool_at: datetime | None = None

    _reject_binary_float = field_validator(
        *_JUPITER_USD_FIELDS,
        "organic_score",
        "top_holders_share",
        "developer_share",
        mode="before",
    )(_exact_decimal)
    _normalize_utc = field_validator("source_at")(_utc)

    @field_validator("first_pool_at")
    @classmethod
    def _normalize_first_pool(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_reading(self) -> "JupiterTokenReading":
        for name in _JUPITER_USD_FIELDS:
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("top_holders_share", "developer_share"):
            value = getattr(self, name)
            if value is not None and not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name} must be a fraction in [0, 1]")
        if self.organic_score is not None and not Decimal(0) <= self.organic_score <= 100:
            raise ValueError("organic_score must be in [0, 100]")
        for name in ("trades_5m", "traders_5m", "holder_count"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        return self


class RugCheckReading(BaseModel):
    """RugCheck's summary report of one token (``/v1/tokens/{mint}/report/summary``, T22).

    ``danger``: the names of the risks RugCheck rates ``danger`` (a whale holder, mint
    authority, unlocked LP); ``warnings``: the names of every other risk it lists.
    ``score`` is ``score_normalised`` (0..100, higher is riskier) and ``lp_locked_share``
    the locked share of the token's LP in [0, 1]. ``detail`` says why a report is
    unavailable.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    mint: PublicKey
    provider: str = "rugcheck"
    source_at: datetime
    freshness: Freshness
    detail: str | None = None
    score: int | None = None
    danger: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    lp_locked_share: Decimal | None = None

    _reject_binary_float = field_validator("lp_locked_share", mode="before")(_exact_decimal)
    _normalize_utc = field_validator("source_at")(_utc)

    @model_validator(mode="after")
    def validate_report(self) -> "RugCheckReading":
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError("score must be in [0, 100]")
        if self.lp_locked_share is not None and not (
            Decimal(0) <= self.lp_locked_share <= Decimal(1)
        ):
            raise ValueError("lp_locked_share must be a fraction in [0, 1]")
        return self


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
    # The enhanced provider's reading for this token as received (GMGN, G18), with its own
    # ``source_at`` and freshness; ``None`` when no enhanced source was consulted.
    enhanced: EnhancedTokenSignals | None = None
    # D09: ``False`` when no on-chain source is configured at all (no RPC): every base
    # signal is unknown by deployment, not by a failure, and the snapshot is UNAVAILABLE.
    source_configured: bool = True
    # The free token sources as received (T22), each with its own source time and
    # freshness; ``None`` when the source was not asked for this token.
    jupiter: JupiterTokenReading | None = None
    rugcheck: RugCheckReading | None = None

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
        if not self.source_configured and self.data_quality is not DataQuality.UNAVAILABLE:
            raise ValueError("a token without a configured source has no data")
        if self.source_at > self.observed_at or self.observed_at > self.ingested_at:
            raise ValueError("risk timestamps must be source <= observed <= ingested")
        return self


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
