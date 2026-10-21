"""Exact, immutable risk state and decision contracts."""

from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, SkipValidation, field_validator, model_validator

from src.domain.strategy.models import StrategyPlan


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("risk decimals must not use binary float")
    return value


class RiskReason(StrEnum):
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DAILY_STOP_REACHED = "DAILY_STOP_REACHED"
    POSITION_LIMIT_REACHED = "POSITION_LIMIT_REACHED"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    ZERO_CAPACITY = "ZERO_CAPACITY"
    INVALID_LOSS_ESTIMATE = "INVALID_LOSS_ESTIMATE"
    ALLOCATION_CAP = "ALLOCATION_CAP"
    POSITION_RISK_CAP = "POSITION_RISK_CAP"
    LIQUIDITY_CAP = "LIQUIDITY_CAP"
    BALANCE_CAP = "BALANCE_CAP"
    SIZED_PLAN_INVARIANT_BREACH = "SIZED_PLAN_INVARIANT_BREACH"


class RiskDecisionStatus(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class RiskPolicy(BaseModel):
    """Configurable limits constrained by the operator's non-negotiable maxima."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ruleset_version: str
    config_version: str
    maximum_allocation_fraction: Decimal = Decimal("0.10")
    maximum_position_risk_fraction: Decimal = Decimal("0.01")
    maximum_open_positions: int = 3
    daily_stop_fraction: Decimal = Decimal("0.04")
    maximum_liquidity_fraction: Decimal = Decimal("0.10")
    fee_reserve_base_units: int = 0

    _reject_float = field_validator(
        "maximum_allocation_fraction",
        "maximum_position_risk_fraction",
        "daily_stop_fraction",
        "maximum_liquidity_fraction",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_limits(self) -> "RiskPolicy":
        if not self.ruleset_version or not self.config_version:
            raise ValueError("risk versions are required")
        if not Decimal(0) < self.maximum_allocation_fraction <= Decimal("0.10"):
            raise ValueError("allocation fraction must be in (0, 0.10]")
        if not Decimal(0) < self.maximum_position_risk_fraction <= Decimal("0.01"):
            raise ValueError("position risk fraction must be in (0, 0.01]")
        if not 1 <= self.maximum_open_positions <= 3:
            raise ValueError("maximum_open_positions must be between 1 and 3")
        if not Decimal(0) < self.daily_stop_fraction <= Decimal("0.04"):
            raise ValueError("daily stop fraction must be in (0, 0.04]")
        if not Decimal(0) < self.maximum_liquidity_fraction <= Decimal(1):
            raise ValueError("liquidity fraction must be in (0, 1]")
        if self.fee_reserve_base_units < 0:
            raise ValueError("fee reserve cannot be negative")
        return self


class PortfolioState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    equity_base_units: int
    available_balance_base_units: int
    open_position_count: int

    @model_validator(mode="after")
    def validate_portfolio(self) -> "PortfolioState":
        if self.equity_base_units <= 0:
            raise ValueError("equity must be positive")
        if self.available_balance_base_units < 0 or self.open_position_count < 0:
            raise ValueError("portfolio balances and counts cannot be negative")
        return self


class LiquidityState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    depth_base_units: int
    price_impact_cap_base_units: int

    @model_validator(mode="after")
    def validate_liquidity(self) -> "LiquidityState":
        if self.depth_base_units < 0 or self.price_impact_cap_base_units < 0:
            raise ValueError("liquidity capacities cannot be negative")
        return self


class SizingCaps(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    allocation_base_units: int
    risk_base_units: int
    liquidity_base_units: int
    balance_base_units: int

    @model_validator(mode="after")
    def validate_caps(self) -> "SizingCaps":
        if any(value < 0 for value in self.model_dump().values()):
            raise ValueError("sizing caps cannot be negative")
        return self


class SizingBasis(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    equity_base_units: int
    available_balance_base_units: int
    depth_base_units: int
    price_impact_cap_base_units: int
    fee_reserve_base_units: int
    risk_budget_base_units: int

    @model_validator(mode="after")
    def validate_basis(self) -> "SizingBasis":
        values = self.model_dump().values()
        if self.equity_base_units <= 0 or any(value < 0 for value in values):
            raise ValueError("sizing basis values are invalid")
        return self


class SizedPlan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plan: StrategyPlan
    size_base_units: int
    estimated_loss_base_units: int
    caps: SizingCaps
    basis: SizingBasis
    binding_caps: tuple[RiskReason, ...]
    ruleset_version: str
    config_version: str
    correlation_id: str

    @model_validator(mode="after")
    def validate_sized_plan(self) -> "SizedPlan":
        if self.size_base_units <= 0:
            raise ValueError("sized plan must have positive size")
        if self.size_base_units > min(self.caps.model_dump().values()):
            raise ValueError("sized plan exceeds a sizing cap")
        cap_reasons = (
            (RiskReason.ALLOCATION_CAP, self.caps.allocation_base_units),
            (RiskReason.POSITION_RISK_CAP, self.caps.risk_base_units),
            (RiskReason.LIQUIDITY_CAP, self.caps.liquidity_base_units),
            (RiskReason.BALANCE_CAP, self.caps.balance_base_units),
        )
        expected_binding = tuple(
            reason for reason, value in cap_reasons if value == self.size_base_units
        )
        if self.binding_caps != expected_binding:
            raise ValueError("binding caps do not match sized amount")
        expected_loss = int(
            (
                Decimal(self.size_base_units) * self.plan.loss_fraction_at_invalidation
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        if self.estimated_loss_base_units != expected_loss:
            raise ValueError("estimated loss must be conservatively rounded")
        if self.estimated_loss_base_units > self.basis.risk_budget_base_units:
            raise ValueError("estimated loss exceeds the recorded risk budget")
        if (
            self.ruleset_version != self.plan.ruleset_version
            or self.config_version != self.plan.config_version
            or self.correlation_id != self.plan.correlation_id
        ):
            raise ValueError("sized plan identity differs from strategy plan")
        return self


class RiskRejection(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    reasons: tuple[RiskReason, ...]
    caps: SizingCaps | None
    ruleset_version: str
    config_version: str
    correlation_id: str


class RiskState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    as_of: datetime
    risk_day_start: datetime
    start_of_day_equity_base_units: int
    realized_pnl_base_units: int
    conservative_unrealized_pnl_base_units: int
    open_position_count: int
    kill_switch_active: bool = False

    @model_validator(mode="after")
    def validate_state(self) -> "RiskState":
        if self.as_of.tzinfo is None or self.risk_day_start.tzinfo is None:
            raise ValueError("risk timestamps must be timezone-aware")
        if self.risk_day_start > self.as_of:
            raise ValueError("risk day cannot begin in the future")
        if self.start_of_day_equity_base_units <= 0:
            raise ValueError("start-of-day equity must be positive")
        if self.open_position_count < 0:
            raise ValueError("open_position_count cannot be negative")
        return self

    @property
    def conservative_daily_pnl_base_units(self) -> int:
        return self.realized_pnl_base_units + min(self.conservative_unrealized_pnl_base_units, 0)


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: RiskDecisionStatus
    reasons: tuple[RiskReason, ...]
    plan: SkipValidation[SizedPlan]
    ruleset_version: str
    config_version: str
    correlation_id: str


class RiskActionKind(StrEnum):
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    ALERT = "ALERT"


class RiskAction(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: RiskActionKind
    reason: RiskReason
    position_id: str | None = None


class PromotionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    full_shadow_days: int
    completed_positions: int
    net_expectancy_positive: bool
    maximum_drawdown: Decimal
    data_coverage: Decimal
    open_critical_alerts: int
    operator_confirmed: bool

    _reject_float = field_validator("maximum_drawdown", "data_coverage", mode="before")(
        _exact_decimal
    )


class PromotionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    allowed: bool
    reasons: tuple[str, ...]
