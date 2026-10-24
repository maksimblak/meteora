"""Immutable fill, ledger and receipt records shared by shadow and live lifecycles."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.models.events import EventType
from src.execution.contracts import DlmmCommandV1, ExecutionPhase, GatewayReceiptV1, GatewayState
from src.portfolio.models import PositionValuation, ShadowPosition


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float is forbidden for accounting values")
    return value


class CostModel(BaseModel):
    """Conservative per-fill cost estimate. ``fee_income_base_units`` is an accrual the caller
    adds to a fill; the shadow broker books the position's share of the pool's LP fees
    since its last fill on top of it (``shadow_fee_income``)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    slippage_bps: int
    network_fee_base_units: Decimal
    jito_tip_base_units: Decimal
    fee_income_base_units: Decimal = Decimal(0)

    _reject_float = field_validator(
        "network_fee_base_units", "jito_tip_base_units", "fee_income_base_units", mode="before"
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_costs(self) -> "CostModel":
        if not 0 <= self.slippage_bps <= 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if (
            min(self.network_fee_base_units, self.jito_tip_base_units, self.fee_income_base_units)
            < 0
        ):
            raise ValueError("cost model values cannot be negative")
        return self


class Fill(BaseModel):
    """Execution facts applied to a position, whether modeled (shadow) or confirmed (live)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    filled_at: datetime
    price: Decimal
    slippage_bps: int
    network_fee_base_units: Decimal
    jito_tip_base_units: Decimal
    fee_income_base_units: Decimal

    _reject_float = field_validator(
        "price",
        "network_fee_base_units",
        "jito_tip_base_units",
        "fee_income_base_units",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_fill(self) -> "Fill":
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("fill timestamp must be timezone-aware")
        if self.price <= 0:
            raise ValueError("fill price must be positive")
        if not 0 <= self.slippage_bps <= 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if (
            min(self.network_fee_base_units, self.jito_tip_base_units, self.fee_income_base_units)
            < 0
        ):
            raise ValueError("fill costs and income cannot be negative")
        return self


class LedgerKind(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    REMOVE = "REMOVE"
    CLOSE = "CLOSE"


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    entry_id: str
    command_id: str
    idempotency_key: str
    correlation_id: str
    position_id: str
    kind: LedgerKind
    occurred_at: datetime
    price: Decimal
    capital_delta_base_units: Decimal
    cost_basis_delta_base_units: Decimal
    realized_pnl_base_units: Decimal
    divergence_realized_base_units: Decimal
    fee_income_base_units: Decimal
    slippage_base_units: Decimal
    network_base_units: Decimal
    jito_base_units: Decimal


class LifecycleStep(BaseModel):
    """Result of applying one fill: the next position version and its ledger facts."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position: ShadowPosition
    kind: LedgerKind
    capital_delta_base_units: Decimal
    cost_basis_delta_base_units: Decimal
    realized_pnl_base_units: Decimal
    divergence_realized_base_units: Decimal
    slippage_base_units: Decimal


class ShadowReceipt(BaseModel):
    """Confirmed receipt of a shadow fill in the same phase/receipt shape as live execution."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    command: DlmmCommandV1
    command_digest: str
    fill: Fill
    fill_digest: str
    applied_at: datetime
    phase: ExecutionPhase
    gateway_receipt: GatewayReceiptV1
    position: ShadowPosition
    valuation: PositionValuation
    ledger_entry_ids: tuple[str, ...]
    event_types: tuple[EventType, ...]

    @model_validator(mode="after")
    def validate_receipt(self) -> "ShadowReceipt":
        if self.phase is not ExecutionPhase.CONFIRMED:
            raise ValueError("shadow receipts are recorded only once confirmed")
        receipt = self.gateway_receipt
        if (
            receipt.state is not GatewayState.CONFIRMED
            or receipt.command_id != self.command.command_id
            or receipt.command_digest != self.command_digest
            or receipt.simulation_digest != self.fill_digest
            or receipt.idempotency_key != self.command.idempotency_key
        ):
            raise ValueError("gateway receipt is not bound to the shadow command")
        return self
