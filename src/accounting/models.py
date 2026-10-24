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
    since its last fill on top of it (``shadow_fee_income``).

    ``priority_fee_lamports`` (spec 6.8, No 49): the priority fee of one transaction - the
    worker's estimate (guide-v1 ``shadow_priority_fee_lamports``), never above the executor's
    ceiling ``PRIORITY_FEE_MAX_LAMPORTS``. Lamports are quote base units only in a pool quoted
    in wrapped SOL; the broker books it there, with the tip, on the entry, the exit and the
    sale (``priority_fee_base_units``), and flags it unpriced elsewhere (``cost_model_flags``)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    slippage_bps: int
    network_fee_base_units: Decimal
    jito_tip_base_units: Decimal
    fee_income_base_units: Decimal = Decimal(0)
    priority_fee_lamports: int = 0

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
        if self.priority_fee_lamports < 0:
            raise ValueError("priority_fee_lamports cannot be negative")
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
    # The sale of base tokens an exit returned (G22), booked with the exit that closes.
    SWAP = "SWAP"


class SaleFill(BaseModel):
    """One confirmed sale of the base token an exit of the position returned, by its
    receipt (G22): ``sold_base_units`` of X in the ledger's scale (Y's decimals) for
    ``proceeds_base_units`` of Y. ``mark_price`` is the price the exit booked that X at,
    so the sale moves the position's P&L by ``proceeds - sold * mark`` less its fees."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    command_id: str
    idempotency_key: str
    sold_at: datetime
    sold_base_units: Decimal
    proceeds_base_units: Decimal
    mark_price: Decimal
    network_fee_base_units: Decimal
    priority_fee_base_units: Decimal

    _reject_float = field_validator(
        "sold_base_units",
        "proceeds_base_units",
        "mark_price",
        "network_fee_base_units",
        "priority_fee_base_units",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_sale(self) -> "SaleFill":
        if self.sold_at.tzinfo is None or self.sold_at.utcoffset() is None:
            raise ValueError("sale timestamp must be timezone-aware")
        if self.mark_price <= 0:
            raise ValueError("the exit mark price must be positive")
        if (
            min(
                self.sold_base_units,
                self.proceeds_base_units,
                self.network_fee_base_units,
                self.priority_fee_base_units,
            )
            < 0
        ):
            raise ValueError("sale amounts and fees cannot be negative")
        return self


# The highest base fee the guide names for new coins (2-5 %, spec 6.8): a SHADOW sale in a
# pool whose fee the snapshot does not carry is not modeled cheaper than that.
UNKNOWN_POOL_FEE_RATE = Decimal("0.05")


class ExitSaleModel(BaseModel):
    """SHADOW model of the sale after an exit (G22).

    The base token the conservative valuation says an exit returns (a tranche below its
    entry price is all X) is sold at the fill price less the pool's base fee
    (``base_fee_rate`` of the snapshot, ``unknown_pool_fee_rate`` without it) and the
    cost model's slippage as the quote's impact, in one more transaction (network fee and
    tip of the cost model). ``enabled`` mirrors ``SWAP_AFTER_EXIT``."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    enabled: bool = True
    unknown_pool_fee_rate: Decimal = UNKNOWN_POOL_FEE_RATE

    _reject_float = field_validator("unknown_pool_fee_rate", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_model(self) -> "ExitSaleModel":
        if not Decimal(0) <= self.unknown_pool_fee_rate <= Decimal(1):
            raise ValueError("pool fee rate must be a fraction in [0, 1]")
        return self


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
