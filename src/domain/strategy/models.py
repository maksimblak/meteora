"""Typed strategy plans and position-monitoring records."""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.screening.models import EvidenceItem
from src.market.models import RiskSignal


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("strategy decimals must not use binary float")
    return value


class StrategyKind(StrEnum):
    SPOT = "SPOT"
    BID_ASK = "BID_ASK"


class Direction(StrEnum):
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class StrategySelectionReason(StrEnum):
    """Why a plan uses its strategy; stored on the plan record next to its evidence."""

    LEGACY_NEUTRAL_SPOT = "LEGACY_NEUTRAL_SPOT"
    LEGACY_DIRECTIONAL_BID_ASK = "LEGACY_DIRECTIONAL_BID_ASK"
    BID_ASK_FROM_LOCAL_HIGH = "BID_ASK_FROM_LOCAL_HIGH"
    SPOT_ATH_BREAKOUT = "SPOT_ATH_BREAKOUT"
    SPOT_OVERVOLUME = "SPOT_OVERVOLUME"


class GuideStrategyRules(BaseModel):
    """LexDollar strategy choice (spec 6.6, G15) — a shadow hypothesis, not a promise.

    Default is Bid-Ask from the local high: liquidity from the high down to
    ``bid_ask_target_depth`` below the current price, inside one DLMM position
    (``maximum_position_bins``, the SDK's 70-bin default width). Spot is allowed only on a
    pool all-time-high breakout with working, organic volume, or on overvolume with a hard
    holding limit and an exit on the first volume drop.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    bid_ask_minimum_depth: Decimal = Decimal("0.70")
    bid_ask_target_depth: Decimal = Decimal("0.75")
    bid_ask_maximum_depth: Decimal = Decimal("0.80")
    maximum_position_bins: int = 70
    spot_minimum_volume_per_minute: Decimal = Decimal("14000")
    spot_minimum_fee_efficiency: Decimal = Decimal("0.5")
    overvolume_volume_per_minute: Decimal = Decimal("100000")
    ath_breakout_margin: Decimal = Decimal("0")
    spot_holding_horizon: timedelta = timedelta(hours=1)
    overvolume_maximum_holding_period: timedelta = timedelta(minutes=30)
    overvolume_volume_decay_ratio: Decimal = Decimal("1")
    # The bid-only range ends at the entry bin: a price above it leaves the position all in
    # the quote token, earning nothing. It exits once the price has stayed above the entry
    # bin's top for ``bid_only_upside_grace`` or runs ``bid_only_upside_exit`` beyond it.
    bid_only_upside_grace: timedelta = timedelta(minutes=30)
    bid_only_upside_exit: Decimal = Decimal("0.05")

    _reject_float = field_validator(
        "bid_ask_minimum_depth",
        "bid_ask_target_depth",
        "bid_ask_maximum_depth",
        "spot_minimum_volume_per_minute",
        "spot_minimum_fee_efficiency",
        "overvolume_volume_per_minute",
        "ath_breakout_margin",
        "overvolume_volume_decay_ratio",
        "bid_only_upside_exit",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_guide(self) -> "GuideStrategyRules":
        if not (
            Decimal(0)
            < self.bid_ask_minimum_depth
            <= self.bid_ask_target_depth
            <= self.bid_ask_maximum_depth
            < Decimal(1)
        ):
            raise ValueError("bid-ask depths must satisfy 0 < minimum <= target <= maximum < 1")
        if self.maximum_position_bins < 3:
            raise ValueError("maximum_position_bins must allow a range around the active bin")
        if any(
            value < 0
            for value in (
                self.spot_minimum_volume_per_minute,
                self.spot_minimum_fee_efficiency,
                self.overvolume_volume_per_minute,
                self.ath_breakout_margin,
            )
        ):
            raise ValueError("spot thresholds cannot be negative")
        if self.spot_holding_horizon <= timedelta(0) or (
            self.overvolume_maximum_holding_period <= timedelta(0)
        ):
            raise ValueError("spot holding periods must be positive")
        if not Decimal(0) < self.overvolume_volume_decay_ratio <= Decimal(1):
            raise ValueError("overvolume_volume_decay_ratio must be in (0, 1]")
        if self.bid_only_upside_grace <= timedelta(0) or self.bid_only_upside_exit <= 0:
            raise ValueError("the bid-only upside exit needs a positive grace and distance")
        return self


class StrategyPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ruleset_version: str
    config_version: str
    plan_ttl: timedelta = timedelta(minutes=5)
    holding_horizon: timedelta = timedelta(hours=4)
    reference_horizon: timedelta = timedelta(hours=1)
    maximum_range_exit_probability: Decimal = Decimal("0.10")
    volatility_multiplier: Decimal = Decimal("3")
    directional_momentum_threshold: Decimal = Decimal("0.01")
    directional_confirmation_ratio: Decimal = Decimal("1.5")
    minimum_bins: int = 2
    maximum_bins: int = 50
    loss_fraction_floor: Decimal = Decimal("0.05")
    maximum_loss_fraction: Decimal = Decimal("0.50")
    expected_cost_rate: Decimal = Decimal("0.002")
    volume_decay_ratio: Decimal = Decimal("0.50")
    price_shock_fraction: Decimal = Decimal("0.10")
    guide: GuideStrategyRules | None = None

    _reject_float = field_validator(
        "volatility_multiplier",
        "maximum_range_exit_probability",
        "directional_momentum_threshold",
        "directional_confirmation_ratio",
        "loss_fraction_floor",
        "maximum_loss_fraction",
        "expected_cost_rate",
        "volume_decay_ratio",
        "price_shock_fraction",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_policy(self) -> "StrategyPolicy":
        if not self.ruleset_version or not self.config_version:
            raise ValueError("strategy versions are required")
        if (
            self.plan_ttl <= timedelta(0)
            or self.holding_horizon <= timedelta(0)
            or self.reference_horizon <= timedelta(0)
        ):
            raise ValueError("plan and holding durations must be positive")
        if not 1 <= self.minimum_bins <= self.maximum_bins:
            raise ValueError("bin bounds are inconsistent")
        if any(
            value <= 0
            for value in (
                self.volatility_multiplier,
                self.directional_momentum_threshold,
                self.directional_confirmation_ratio,
                self.loss_fraction_floor,
                self.maximum_loss_fraction,
            )
        ):
            raise ValueError("strategy thresholds must be positive")
        if self.loss_fraction_floor > self.maximum_loss_fraction:
            raise ValueError("loss fraction floor exceeds maximum")
        if not Decimal(0) < self.maximum_range_exit_probability <= Decimal("0.50"):
            raise ValueError("maximum_range_exit_probability must be in (0, 0.50]")
        if not Decimal(0) <= self.expected_cost_rate <= Decimal(1):
            raise ValueError("expected_cost_rate must be in [0, 1]")
        if not Decimal(0) < self.volume_decay_ratio <= Decimal(1):
            raise ValueError("volume_decay_ratio must be in (0, 1]")
        if not Decimal(0) < self.price_shock_fraction <= Decimal(1):
            raise ValueError("price_shock_fraction must be in (0, 1]")
        return self


class InvalidationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    maximum_adverse_price_move: Decimal
    volume_decay_ratio: Decimal
    expires_at: datetime

    @model_validator(mode="after")
    def validate_invalidation(self) -> "InvalidationPolicy":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("invalidation expiry must be timezone-aware")
        if not Decimal(0) < self.maximum_adverse_price_move <= Decimal(1):
            raise ValueError("maximum adverse move must be in (0, 1]")
        if not Decimal(0) < self.volume_decay_ratio <= Decimal(1):
            raise ValueError("volume decay ratio must be in (0, 1]")
        return self


class ExitPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    maximum_holding_period: timedelta
    exit_on_range_breach: bool = True
    exit_on_token_risk_deterioration: bool = True
    compare_rebalance_after_costs: bool = True
    # A bid-only range (nothing above the entry bin): the price above it is a breach only
    # after ``upside_breach_grace`` above the entry bin's top or ``upside_breach_fraction``
    # beyond it. ``None``: any price above the range is a breach at once.
    upside_breach_grace: timedelta | None = None
    upside_breach_fraction: Decimal | None = None

    _reject_float = field_validator("upside_breach_fraction", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_exit(self) -> "ExitPolicy":
        if (self.upside_breach_grace is None) != (self.upside_breach_fraction is None):
            raise ValueError("the upside exit needs both its grace and its distance")
        return self


class StrategyPlan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    strategy: StrategyKind
    direction: Direction
    lower_bin_offset: int
    upper_bin_offset: int
    holding_horizon: timedelta
    range_exit_probability: Decimal
    invalidation: InvalidationPolicy
    exit_policy: ExitPolicy
    created_at: datetime
    expires_at: datetime
    loss_fraction_at_invalidation: Decimal
    expected_cost_rate: Decimal
    ruleset_version: str
    config_version: str
    correlation_id: str
    selection_reason: StrategySelectionReason | None = None
    selection_evidence: tuple[EvidenceItem, ...] = ()

    _reject_float = field_validator(
        "range_exit_probability",
        "loss_fraction_at_invalidation",
        "expected_cost_rate",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_plan(self) -> "StrategyPlan":
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("strategy timestamps must be timezone-aware")
        if self.lower_bin_offset >= self.upper_bin_offset:
            raise ValueError("strategy range must have positive width")
        if self.expires_at <= self.created_at:
            raise ValueError("strategy plan must expire after creation")
        if not Decimal(0) < self.loss_fraction_at_invalidation <= Decimal(1):
            raise ValueError("loss fraction must be in (0, 1]")
        if not Decimal(0) < self.range_exit_probability <= Decimal("0.50"):
            raise ValueError("range exit probability must be in (0, 0.50]")
        return self


class PositionState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    opened_at: datetime
    entry_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    entry_short_volume: Decimal
    maximum_holding_period: timedelta
    # The plan's own exit on volume decay (``InvalidationPolicy.volume_decay_ratio``): an
    # overvolume Spot exits at the first drop (1). ``None``: the monitor's general ratio.
    volume_decay_ratio: Decimal | None = None
    # The plan's upside exit of a bid-only range (``ExitPolicy``); ``None`` for other plans.
    upside_breach_grace: timedelta | None = None
    upside_breach_fraction: Decimal | None = None

    _reject_float = field_validator(
        "entry_price",
        "lower_price",
        "upper_price",
        "entry_short_volume",
        "volume_decay_ratio",
        "upside_breach_fraction",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_position(self) -> "PositionState":
        if not self.position_id:
            raise ValueError("position_id is required")
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("opened_at must be timezone-aware")
        if not Decimal(0) < self.lower_price < self.upper_price:
            raise ValueError("position price range is invalid")
        if self.entry_price <= 0 or self.entry_short_volume < 0:
            raise ValueError("entry values are invalid")
        if self.maximum_holding_period <= timedelta(0):
            raise ValueError("maximum_holding_period must be positive")
        if self.volume_decay_ratio is not None and not (
            Decimal(0) < self.volume_decay_ratio <= Decimal(1)
        ):
            raise ValueError("volume_decay_ratio must be in (0, 1]")
        if (self.upside_breach_grace is None) != (self.upside_breach_fraction is None):
            raise ValueError("the upside exit needs both its grace and its distance")
        return self


class PositionActionKind(StrEnum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    ALERT = "ALERT"


class PositionReason(StrEnum):
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    RANGE_BREACH = "RANGE_BREACH"
    MAX_HOLDING_PERIOD = "MAX_HOLDING_PERIOD"
    VOLUME_DECAY = "VOLUME_DECAY"
    FEE_DECAY = "FEE_DECAY"
    TVL_COMPETITION = "TVL_COMPETITION"
    NEAR_RANGE_EDGE = "NEAR_RANGE_EDGE"
    PRICE_SHOCK = "PRICE_SHOCK"
    TOKEN_RISK_DETERIORATED = "TOKEN_RISK_DETERIORATED"  # noqa: S105
    TOKEN_RISK_UNKNOWN = "TOKEN_RISK_UNKNOWN"  # noqa: S105
    MARKET_DATA_UNSAFE = "MARKET_DATA_UNSAFE"
    FEE_SIGNAL_UNKNOWN = "FEE_SIGNAL_UNKNOWN"
    REBALANCE_AFTER_COSTS = "REBALANCE_AFTER_COSTS"
    CLOSE_BEATS_REBALANCE = "CLOSE_BEATS_REBALANCE"


class PositionSignals(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    current_fee_tvl: Decimal | None
    baseline_fee_tvl: Decimal
    competing_tvl_growth: Decimal
    normalized_range_position: Decimal
    token_risk: RiskSignal
    estimated_rebalance_cost_base_units: int
    estimated_close_cost_base_units: int
    expected_remaining_fee_income_base_units: int
    # Since when the price has stayed above a bid-only range (``PositionMonitor.above_range``)
    # without a break; ``None`` when it is not above.
    above_range_since: datetime | None = None

    _reject_float = field_validator(
        "current_fee_tvl",
        "baseline_fee_tvl",
        "competing_tvl_growth",
        "normalized_range_position",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_signals(self) -> "PositionSignals":
        if self.current_fee_tvl is not None and self.current_fee_tvl < 0:
            raise ValueError("current_fee_tvl cannot be negative")
        if self.baseline_fee_tvl < 0 or self.competing_tvl_growth < 0:
            raise ValueError("fee and TVL signals cannot be negative")
        if not Decimal(0) <= self.normalized_range_position <= Decimal(1):
            raise ValueError("normalized_range_position must be in [0, 1]")
        if any(
            value < 0
            for value in (
                self.estimated_rebalance_cost_base_units,
                self.estimated_close_cost_base_units,
                self.expected_remaining_fee_income_base_units,
            )
        ):
            raise ValueError("position cost estimates cannot be negative")
        return self


class PositionAction(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: PositionActionKind
    reasons: tuple[PositionReason, ...]
    position_id: str
