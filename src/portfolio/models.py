"""Immutable position, valuation, and performance projections."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.models.trading import TradingMode


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float is forbidden for accounting values")
    return value


def _require_aware(*values: datetime | None) -> None:
    for value in values:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("accounting timestamps must be timezone-aware")


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class LiquidityTranche(BaseModel):
    """Liquidity deposited at one fill price; divergence is measured from that price."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    entry_price: Decimal
    quantity_x: Decimal
    quantity_y: Decimal
    cost_basis_base_units: Decimal

    _reject_float = field_validator(
        "entry_price", "quantity_x", "quantity_y", "cost_basis_base_units", mode="before"
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_tranche(self) -> "LiquidityTranche":
        if self.entry_price <= 0:
            raise ValueError("tranche entry price must be positive")
        if min(self.quantity_x, self.quantity_y, self.cost_basis_base_units) < 0:
            raise ValueError("tranche quantities and basis cannot be negative")
        return self


class ShadowPosition(BaseModel):
    """Lifecycle state of one position; identical for shadow fills and live receipts."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    pool_address: str
    status: PositionStatus
    mode: TradingMode
    ruleset_version: str
    correlation_id: str
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    tranches: tuple[LiquidityTranche, ...]
    realized_proceeds_base_units: Decimal = Decimal(0)
    realized_hodl_proceeds_base_units: Decimal = Decimal(0)
    realized_cost_basis_base_units: Decimal = Decimal(0)
    fee_income_base_units: Decimal = Decimal(0)
    slippage_base_units: Decimal = Decimal(0)
    network_base_units: Decimal = Decimal(0)
    jito_base_units: Decimal = Decimal(0)
    version: int = 1

    _reject_float = field_validator(
        "realized_proceeds_base_units",
        "realized_hodl_proceeds_base_units",
        "realized_cost_basis_base_units",
        "fee_income_base_units",
        "slippage_base_units",
        "network_base_units",
        "jito_base_units",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_position(self) -> "ShadowPosition":
        if not self.position_id or not self.ruleset_version or not self.correlation_id:
            raise ValueError("position identity, ruleset and correlation are required")
        _require_aware(self.opened_at, self.updated_at, self.closed_at)
        if self.updated_at < self.opened_at:
            raise ValueError("position update precedes open")
        if self.status is PositionStatus.CLOSED:
            if self.closed_at is None or self.tranches:
                raise ValueError("closed position requires closed_at and no open liquidity")
        elif self.closed_at is not None or not self.tranches:
            raise ValueError("open position requires liquidity and no closed_at")
        if any(
            value < 0
            for value in (
                self.realized_proceeds_base_units,
                self.realized_hodl_proceeds_base_units,
                self.realized_cost_basis_base_units,
                self.fee_income_base_units,
                self.slippage_base_units,
                self.network_base_units,
                self.jito_base_units,
            )
        ):
            raise ValueError("income, proceeds and cost accumulators cannot be negative")
        if self.version < 1:
            raise ValueError("position version must be positive")
        return self

    @property
    def open_cost_basis_base_units(self) -> Decimal:
        return sum((tranche.cost_basis_base_units for tranche in self.tranches), Decimal(0))

    @property
    def invested_base_units(self) -> Decimal:
        return self.realized_cost_basis_base_units + self.open_cost_basis_base_units

    @property
    def total_costs_base_units(self) -> Decimal:
        return self.slippage_base_units + self.network_base_units + self.jito_base_units


class PositionValuation(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    valued_at: datetime
    market_price: Decimal
    invested_base_units: Decimal
    liquidity_value_base_units: Decimal
    hodl_value_base_units: Decimal
    fee_income_base_units: Decimal
    divergence_loss_base_units: Decimal
    slippage_base_units: Decimal
    network_base_units: Decimal
    jito_base_units: Decimal
    total_costs_base_units: Decimal
    realized_pnl_base_units: Decimal
    unrealized_pnl_base_units: Decimal
    net_pnl_base_units: Decimal
    hodl_pnl_base_units: Decimal
    excess_vs_hodl_base_units: Decimal

    _reject_float = field_validator(
        "market_price",
        "invested_base_units",
        "liquidity_value_base_units",
        "hodl_value_base_units",
        "fee_income_base_units",
        "divergence_loss_base_units",
        "slippage_base_units",
        "network_base_units",
        "jito_base_units",
        "total_costs_base_units",
        "realized_pnl_base_units",
        "unrealized_pnl_base_units",
        "net_pnl_base_units",
        "hodl_pnl_base_units",
        "excess_vs_hodl_base_units",
        mode="before",
    )(_exact_decimal)

    @property
    def conservative_unrealized_pnl_base_units(self) -> Decimal:
        """Unrealized gains never offset realized losses in risk limits."""

        return min(self.unrealized_pnl_base_units, Decimal(0))


class PositionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    ruleset_version: str
    mode: TradingMode
    correlation_id: str
    opened_at: datetime
    closed_at: datetime
    valuation: PositionValuation


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "TimeWindow":
        _require_aware(self.start, self.end)
        if self.end <= self.start:
            raise ValueError("performance window must have positive duration")
        return self

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


class PerformanceGroup(BaseModel):
    """Statistics of one (ruleset, mode) population; populations are never merged."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ruleset_version: str
    mode: TradingMode
    completed_positions: int
    winning_positions: int
    net_pnl_base_units: Decimal
    hodl_pnl_base_units: Decimal
    excess_vs_hodl_base_units: Decimal
    fee_income_base_units: Decimal
    divergence_loss_base_units: Decimal
    total_costs_base_units: Decimal
    expectancy_base_units: Decimal
    maximum_drawdown_base_units: Decimal
    first_closed_at: datetime
    last_closed_at: datetime


class PerformanceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    window: TimeWindow
    groups: tuple[PerformanceGroup, ...]
