"""The money path: admitted entries, their durable continuation and gateway exits.

Order of guarantees of an entry (each step fails closed and never re-orders):

1. ``claim_entry`` - PENDING -> EXECUTING under the kill-switch / daily-stop guards;
2. *guard* - the durable mode state is re-read; kill switch, daily stop or a mode
   change since the claim blocks the command (closes the claim -> submit seam);
3. *audit* - a ``CommandSubmitted`` record and the position context are made durable;
   an unaudited command is never sent and the failure raises a critical alert. A
   gateway OPEN has no position until the executor simulates it (contract V2: the
   executor creates the position key), so its context is written right after that
   simulation, under the address the executor named, and before anything is sent;
4. *submit* - the submitter calls the guard again immediately before the one
   irreversible step (shadow fill or gateway submit, after any simulation), and the
   execution service re-reads the kill switch right before ``gateway.submit``;
5. *settle* - CONFIRMED -> ``confirm_entry``; FAILED/BLOCKED -> ``fail_entry`` only
   while the durable execution record proves nothing left; an unknown outcome stays
   EXECUTING (capacity reserved) until it is reconciled and is never resent.

Non-SHADOW work that outlives one cycle continues from durable state only:
``GatewayEntrySettler`` executes approved entries and settles reconciled ones,
``GatewayPositionManager`` exits and reduces non-SHADOW positions through a fresh
simulation and the gateway, one bounded sequence of attempts per position, under the
kill-switch policy recorded in the mode state, and sells the base token each confirmed
exit returned (G22, ``SWAP_AFTER_EXIT``): a CLOSE is booked - the position closed in
quote - only once its sale has ended, sold or explicitly left unsold under an alert.
"""

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.accounting.models import CostModel, Fill, LedgerEntry, SaleFill, ShadowReceipt
from src.accounting.repository import AccountingRepository
from src.accounting.service import AccountingService, ShadowBroker, position_aggregate_id
from src.core.clock import Clock
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.risk.models import SizedPlan
from src.domain.strategy import (
    PositionActionKind,
    StrategyPlan,
    StrategyRejection,
    StrategySelectionReason,
)
from src.execution.contracts import (
    DlmmCommand,
    DlmmCommandV1,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    ExecutionState,
    GatewayReceipt,
    GatewayReceiptV1,
    GatewayReceiptV2,
    GatewayState,
    LiquidityStrategy,
    SettlementV2,
    SimulationResultV2,
    SwapDirection,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionBlocked,
    ExecutionRecord,
    ExecutionService,
    ExecutionStore,
    GatewayRejected,
    SimulationRejected,
    swap_rejection,
)
from src.execution.settings import SwapSettings
from src.market.models import PoolSnapshot, WalletState
from src.modes.alerts import SystemAlertResolver, alert_subject
from src.modes.models import KillSwitchPolicy, PendingPlan, PendingPlanStatus
from src.modes.repository import ModeRepository
from src.observability.alerts import CriticalAlertReporter
from src.observability.logging import error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import (
    AuditLog,
    PositionContext,
    PositionContextStore,
    SnapshotHistory,
)
from src.portfolio.models import PositionStatus, ShadowPosition
from src.providers.contracts import PoolStateReader
from src.worker.policy import DEFAULT_ACTIVE_BIN_TOLERANCE, PipelinePolicy

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BPS = Decimal(10_000)
UNRESOLVED_PHASES = frozenset(
    {ExecutionPhase.SUBMITTING, ExecutionPhase.SUBMITTED, ExecutionPhase.UNKNOWN}
)
_EXECUTABLE_PHASES = frozenset({ExecutionPhase.APPROVED, ExecutionPhase.SIMULATED})
_SPENT_PHASES = frozenset({ExecutionPhase.FAILED, ExecutionPhase.EXPIRED})
POSITION_ACTION_ATTEMPTS = 5
POSITION_ACTION_TTL = timedelta(minutes=2)
REDUCE_BPS = 5_000
_LIQUIDITY_OPERATIONS = frozenset({DlmmOperation.OPEN, DlmmOperation.ADD})
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
_MAXIMUM_DECIMALS = 18


def shadow_address(seed: str) -> str:
    """Deterministic, valid 32-byte base58 key used for shadow wallets and positions."""

    data = hashlib.sha256(seed.encode("utf-8")).digest()
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    padding = len(data) - len(data.lstrip(b"\x00"))
    return "1" * padding + encoded


def entry_command_id(plan_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"meteora-volume-bot:entry-command:{plan_id}")


def entry_idempotency_key(plan_id: UUID) -> str:
    return f"entry:{plan_id}"


def entry_position_address(plan_id: UUID) -> str:
    """The synthetic position of a SHADOW entry; a gateway position comes from the executor."""

    return shadow_address(f"position:{plan_id}")


def position_action_key(position_id: str, attempt: int, preview: int = 1) -> str:
    """``position-action:{position_id}:{attempt}``; a renewed APPROVAL preview of the same
    attempt (the operator let the previous one expire unapproved) adds ``:{preview}``."""

    if attempt < 1 or preview < 1:
        raise ValueError("attempt and preview numbers start at 1")
    key = f"position-action:{position_id}:{attempt}"
    return key if preview == 1 else f"{key}:{preview}"


def position_action_command_id(position_id: str, attempt: int, preview: int = 1) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"meteora-volume-bot:{position_action_key(position_id, attempt, preview)}",
    )


def exit_sale_key(position_id: str, exit_attempt: int, part: int, preview: int = 1) -> str:
    """``exit-sale:{position_id}:{exit_attempt}:{part}``: one sale command of the base token
    the confirmed exit attempt returned; a renewed APPROVAL card of the same part (the
    previous one expired unapproved) adds ``:{preview}``."""

    if exit_attempt < 1 or part < 1 or preview < 1:
        raise ValueError("attempt, part and preview numbers start at 1")
    key = f"exit-sale:{position_id}:{exit_attempt}:{part}"
    return key if preview == 1 else f"{key}:{preview}"


def exit_sale_command_id(
    position_id: str, exit_attempt: int, part: int, preview: int = 1
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"meteora-volume-bot:{exit_sale_key(position_id, exit_attempt, part, preview)}",
    )


# --- entry command ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BinAnchor:
    """On-chain facts a non-SHADOW entry needs that ``PoolSnapshot`` does not carry.

    Without them the strategy's *relative* bin offsets cannot become absolute bin ids
    and sized capital cannot become token base units, so a gateway entry fails closed.
    """

    active_bin_id: int
    base_decimals: int
    quote_decimals: int

    def __post_init__(self) -> None:
        if not (0 <= self.base_decimals <= 18 and 0 <= self.quote_decimals <= 18):
            raise ValueError("token decimals must be in [0, 18]")


type AnchorSource = Callable[[PoolSnapshot], BinAnchor | None]


def snapshot_anchor(market: PoolSnapshot) -> BinAnchor | None:
    """The active bin and both decimals the snapshot carries (D06/D07), or ``None``.

    The sweep's active bin ages within minutes; a non-SHADOW entry re-reads it from the LB
    pair account right before the command is built and never sends this one unchecked.
    """

    values = (market.active_bin_id, market.base_decimals, market.quote_decimals)
    if any(value is None for value in values):
        return None
    active, base, quote = values
    assert active is not None and base is not None and quote is not None
    if base > _MAXIMUM_DECIMALS or quote > _MAXIMUM_DECIMALS:
        return None
    return BinAnchor(active_bin_id=active, base_decimals=base, quote_decimals=quote)


class EntryCommandUnavailable(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_entry_command(
    *,
    plan_id: UUID,
    mode: TradingMode,
    market: PoolSnapshot,
    size_base_units: int,
    strategy_kind: str,
    lower_bin_offset: int,
    upper_bin_offset: int,
    expires_at: datetime,
    correlation_id: str,
    ruleset_version: str,
    wallet_address: str,
    max_slippage_bps: int,
    anchor: BinAnchor | None = None,
    active_bin_tolerance: int = DEFAULT_ACTIVE_BIN_TOLERANCE,
) -> DlmmCommand:
    """Split the sized capital between Y and X by where the range lies (D08).

    Bins below the active bin hold only Y, bins above only X, the active bin both: a range
    with no bin above the active one (the guide's Bid-Ask from the high, active bin funded
    with Y) is all quote token, one with no bin below is all base token, and a range on
    both sides splits by its bins on each side, the active bin shared (an even split for
    a symmetric Spot). The executor never swaps, so a leg the wallet does not hold refuses
    the entry.

    SHADOW: a V1 lifecycle command on the synthetic shadow position with the strategy's
    relative bins (the shadow broker models them).

    Any other mode: a V2 command *without* a position - the executor creates it and the
    receipt names it - anchored on a ``BinAnchor``: bins become ``active_bin_id + offset``
    (the executor refuses the entry when the pool moved more than ``active_bin_tolerance``
    bins) and X is expressed in its own base units through both mint decimals, rounded
    down to a whole number of accounting units (X in Y's scale) so the booked fill is
    exactly what was sent. Without the anchor the command is refused - an offset is
    never sent as an absolute bin id.
    """

    amount_y = quote_leg(size_base_units, lower_bin_offset, upper_bin_offset)
    quote_value_x = Decimal(size_base_units - amount_y)
    try:
        correlation = UUID(correlation_id)
    except ValueError:
        correlation = uuid5(NAMESPACE_URL, f"correlation:{correlation_id}")
    if mode is TradingMode.SHADOW:
        return DlmmCommandV1(
            command_id=entry_command_id(plan_id),
            idempotency_key=entry_idempotency_key(plan_id),
            correlation_id=correlation,
            ruleset_version=ruleset_version,
            mode=mode,
            operation=DlmmOperation.OPEN,
            pool_address=market.pool.address.root,
            wallet_address=wallet_address,
            position_address=entry_position_address(plan_id),
            expires_at=expires_at,
            max_slippage_bps=max_slippage_bps,
            strategy=LiquidityStrategy(strategy_kind),
            min_bin_id=lower_bin_offset,
            max_bin_id=upper_bin_offset,
            amount_x=str(_floor(quote_value_x / market.price)),
            amount_y=str(amount_y),
        )
    if anchor is None:
        raise EntryCommandUnavailable("ENTRY_ANCHOR_UNAVAILABLE")
    if market.price <= 0:
        raise EntryCommandUnavailable("MARKET_PRICE_UNAVAILABLE")
    return DlmmCommandV2(
        command_id=entry_command_id(plan_id),
        idempotency_key=entry_idempotency_key(plan_id),
        correlation_id=correlation,
        ruleset_version=ruleset_version,
        mode=mode,
        operation=DlmmOperation.OPEN,
        pool_address=market.pool.address.root,
        wallet_address=wallet_address,
        expires_at=expires_at,
        max_slippage_bps=max_slippage_bps,
        strategy=LiquidityStrategy(strategy_kind),
        min_bin_id=anchor.active_bin_id + lower_bin_offset,
        max_bin_id=anchor.active_bin_id + upper_bin_offset,
        amount_x=str(_token_amount_x(quote_value_x / market.price, anchor)),
        amount_y=str(amount_y),
        active_bin_id=anchor.active_bin_id,
        active_bin_tolerance=active_bin_tolerance,
        token_x_decimals=anchor.base_decimals,
        token_y_decimals=anchor.quote_decimals,
    )


def quote_leg(size_base_units: int, lower_bin_offset: int, upper_bin_offset: int) -> int:
    """The quote-token (Y) part of ``size_base_units`` for a range of relative bins.

    No bin above the active one: all Y, the active bin included (a one-sided bid). No bin
    below it: all X. Otherwise both sides by their bins, the active bin shared.
    """

    if upper_bin_offset <= 0:
        return size_base_units
    if lower_bin_offset >= 0:
        return 0
    below = Decimal(-lower_bin_offset) + Decimal("0.5")
    width = Decimal(upper_bin_offset - lower_bin_offset + 1)
    return _floor(Decimal(size_base_units) * below / width)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _token_amount_x(accounting_x: Decimal, anchor: BinAnchor) -> int:
    """X base units for ``accounting_x`` (X in Y's scale), a whole number of accounting units."""

    shift = anchor.base_decimals - anchor.quote_decimals
    if shift >= 0:
        return _floor(accounting_x) * _power_of_ten(shift)
    return _floor(accounting_x / _power_of_ten(-shift))


def _power_of_ten(exponent: int) -> int:
    factor: int = 10**exponent
    return factor


def accounting_amount_x(amount_x: int, token_x_decimals: int, token_y_decimals: int) -> int:
    """X base units in the ledger's scale (Y's decimals): ``quantity_x * price`` is then Y."""

    shift = token_y_decimals - token_x_decimals
    if shift >= 0:
        return amount_x * _power_of_ten(shift)
    divisor = _power_of_ten(-shift)
    if amount_x % divisor:
        raise ValueError("amount_x is not a whole number of accounting units")
    return amount_x // divisor


# --- submitters --------------------------------------------------------------------------


class SubmitStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    position_id: str | None = None
    detail: str | None = None


type SubmitGuard = Callable[[], None]
# Makes the position context durable under the executor-named address and returns the entry
# price it recorded (every path books the entry at that one price); raises to stop.
type PositionRecorder = Callable[[str], Awaitable[Decimal]]


class CommandSubmitter(Protocol):
    async def submit(
        self,
        command: DlmmCommand,
        market: PoolSnapshot,
        guard: SubmitGuard,
        record_position: PositionRecorder | None = None,
    ) -> SubmitResult: ...


class ShadowSubmitter:
    """SHADOW: a modeled fill through the T04 shadow broker, no network at all."""

    def __init__(self, accounting: AccountingRepository, clock: Clock, costs: CostModel) -> None:
        self._broker = ShadowBroker(accounting, clock)
        self._costs = costs

    async def submit(
        self,
        command: DlmmCommand,
        market: PoolSnapshot,
        guard: SubmitGuard,
        record_position: PositionRecorder | None = None,
    ) -> SubmitResult:
        del record_position  # a SHADOW position is synthetic; its context precedes the fill
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        if not isinstance(command, DlmmCommandV1):
            return SubmitResult(SubmitStatus.FAILED, detail="SHADOW_REQUIRES_LIFECYCLE_COMMAND")
        try:
            receipt = self._broker.apply(command, market, self._costs)
        except ValueError as error:
            return SubmitResult(SubmitStatus.FAILED, detail=str(error))
        return SubmitResult(SubmitStatus.CONFIRMED, position_id=receipt.position.position_id)


class CommandResolver:
    """``PlanResolver`` keyed by command id; the durable record wins over a staged one.

    A command is staged only for the ``prepare`` call that persists it; afterwards
    every resolution comes from the execution store, so a restarted worker works on
    exactly the persisted payload.
    """

    def __init__(self, store: ExecutionStore) -> None:
        self._store = store
        self._staged: dict[UUID, DlmmCommand] = {}

    def stage(self, command: DlmmCommand) -> UUID:
        self._staged[command.command_id] = command
        return command.command_id

    def discard(self, command_id: UUID) -> None:
        self._staged.pop(command_id, None)

    async def resolve(self, plan_id: UUID) -> DlmmCommand:
        record = await self._store.get(plan_id)
        if record is not None:
            return record.command
        try:
            return self._staged[plan_id]
        except KeyError as error:
            raise ExecutionBlocked("plan not found") from error


class ModeKillSwitch:
    """``KillSwitchProbe`` over the durable mode state, re-read on every call."""

    def __init__(self, modes: ModeRepository) -> None:
        self._modes = modes

    async def kill_switch_active(self) -> bool:
        return self._modes.mode_state().kill_switch_active

    async def kill_switch_policy(self) -> KillSwitchPolicy | None:
        return self._modes.mode_state().active_kill_switch_policy


class GatewayFillBook:
    """Books a CONFIRMED gateway command into T04 accounting, once per idempotency key.

    The fill is the bound simulation's measured price impact at ``price``. Network and
    priority fees are the receipt's chain settlement (base fee as network cost, priority
    fee as the tip) when the settlement names Y as wrapped SOL - lamports are then quote
    base units - whoever books it (the submitter, the entry settler, restart booking, an
    exit or a sale); otherwise, or while the settlement is unknown, the policy cost model.

    The ledger records V1 lifecycle commands. A V2 command is booked as its lifecycle
    form: on the position its receipt names and with X converted by the simulation's
    mint decimals into Y's scale, so ``quantity_x * price + quantity_y`` is Y base units
    for any pair of decimals. ``command_digest`` stays the digest of what was executed.
    A CLOSE carries the sales of the base token its position's exits returned (G22), by
    their receipts, into the same commit; ``exit_sale`` marks the close as sold or not.
    """

    def __init__(
        self,
        accounting: AccountingRepository,
        executions: ExecutionStore,
        clock: Clock,
        costs: CostModel,
    ) -> None:
        self._accounting = accounting
        self._executions = executions
        self._clock = clock
        self._costs = costs
        self._service = AccountingService()

    def fees(self, receipt: GatewayReceipt | None) -> tuple[Decimal, Decimal]:
        """(network, priority) of a confirmed command in quote base units (see the class)."""

        settlement = receipt.settlement if isinstance(receipt, GatewayReceiptV2) else None
        if settlement is None or settlement.mint_y != WRAPPED_SOL_MINT:
            return self._costs.network_fee_base_units, self._costs.jito_tip_base_units
        priority = Decimal(settlement.priority_fee_lamports)
        return Decimal(settlement.fee_lamports) - priority, priority

    async def book(
        self,
        command_id: UUID,
        price: Decimal,
        *,
        sales: Sequence[SaleFill] = (),
        exit_sale: Mapping[str, object] | None = None,
    ) -> ShadowReceipt:
        record = await self._executions.get(command_id)
        if record is None:
            raise ValueError("execution record not found")
        receipt = record.receipt
        if receipt is None or receipt.state is not GatewayState.CONFIRMED:
            raise ValueError("only a confirmed gateway command is booked")
        command = lifecycle_command(record)
        existing = self._accounting.receipt_for(command.idempotency_key)
        if existing is not None:
            if existing.command_digest != receipt.command_digest:
                raise ValueError("idempotency key is bound to another command")
            return existing
        if command.position_address is None:  # pragma: no cover - contract invariant
            raise ValueError("lifecycle commands require position_address")
        current = self._accounting.position_for(command.position_address)
        now = self._clock.now()
        network, priority = self.fees(receipt)
        fill = Fill(
            filled_at=now,
            price=price,
            slippage_bps=record.simulation.price_impact_bps or 0,
            network_fee_base_units=network,
            jito_tip_base_units=priority,
            fee_income_base_units=Decimal(0),
        )
        step = self._service.apply_fill(current, command, fill)
        position = step.position
        sale_entries: tuple[LedgerEntry, ...] = ()
        if sales:
            position, sale_entries = self._service.apply_sales(
                position, sales, str(command.correlation_id)
            )
        valuation = self._service.value_at(position, price, now)
        entry = LedgerEntry(
            entry_id=str(uuid5(NAMESPACE_URL, f"ledger:{command.idempotency_key}")),
            command_id=str(command.command_id),
            idempotency_key=command.idempotency_key,
            correlation_id=str(command.correlation_id),
            position_id=step.position.position_id,
            kind=step.kind,
            occurred_at=now,
            price=price,
            capital_delta_base_units=step.capital_delta_base_units,
            cost_basis_delta_base_units=step.cost_basis_delta_base_units,
            realized_pnl_base_units=step.realized_pnl_base_units,
            divergence_realized_base_units=step.divergence_realized_base_units,
            fee_income_base_units=fill.fee_income_base_units,
            slippage_base_units=step.slippage_base_units,
            network_base_units=fill.network_fee_base_units,
            jito_base_units=fill.jito_tip_base_units,
        )
        events = _booking_events(
            command,
            receipt,
            position,
            now,
            record.simulation.price_impact_bps,
            exit_sale=exit_sale,
        )
        booked = ShadowReceipt(
            command=command,
            command_digest=receipt.command_digest,
            fill=fill,
            # A gateway fill is the bound simulation's fill: its digest is the binding.
            fill_digest=receipt.simulation_digest,
            applied_at=now,
            phase=ExecutionPhase.CONFIRMED,
            gateway_receipt=_lifecycle_receipt(receipt),
            position=position,
            valuation=valuation,
            ledger_entry_ids=(entry.entry_id, *(item.entry_id for item in sale_entries)),
            event_types=tuple(event.event_type for event in events),
        )
        outcome = (
            self._service.close(position, booked)
            if position.status is PositionStatus.CLOSED
            else None
        )
        return self._accounting.commit(
            booked,
            (entry, *sale_entries),
            outcome,
            events,
            expected_version=None if current is None else current.version,
        )


def lifecycle_command(record: ExecutionRecord) -> DlmmCommandV1:
    """The executed command in the ledger's V1 lifecycle shape (see ``GatewayFillBook``)."""

    command = record.command
    if isinstance(command, DlmmCommandV1):
        return command
    if command.operation is DlmmOperation.SWAP:
        raise ValueError("a sale is booked with the exit it settles, never as a lifecycle fill")
    position = record.position_address
    if position is None or not isinstance(record.receipt, GatewayReceiptV2):
        raise ValueError("a V2 command is booked only on the position its receipt names")
    amount_x = command.amount_x
    if command.operation in _LIQUIDITY_OPERATIONS:
        simulation = record.simulation
        if (
            not isinstance(simulation, SimulationResultV2)
            or simulation.token_x_decimals is None
            or simulation.token_y_decimals is None
            or simulation.token_x_decimals != command.token_x_decimals
            or simulation.token_y_decimals != command.token_y_decimals
            or amount_x is None
        ):
            raise ValueError("mint decimals of the fill are unknown or differ from the command")
        amount_x = str(
            accounting_amount_x(
                int(amount_x), simulation.token_x_decimals, simulation.token_y_decimals
            )
        )
    return DlmmCommandV1(
        command_id=command.command_id,
        idempotency_key=command.idempotency_key,
        correlation_id=command.correlation_id,
        ruleset_version=command.ruleset_version,
        mode=command.mode,
        operation=command.operation,
        pool_address=command.pool_address,
        wallet_address=command.wallet_address,
        position_address=position,
        expires_at=command.expires_at,
        max_slippage_bps=command.max_slippage_bps,
        strategy=command.strategy,
        min_bin_id=command.min_bin_id,
        max_bin_id=command.max_bin_id,
        amount_x=amount_x,
        amount_y=command.amount_y,
        remove_bps=command.remove_bps,
    )


def _lifecycle_receipt(receipt: GatewayReceipt) -> GatewayReceiptV1:
    return receipt.lifecycle() if isinstance(receipt, GatewayReceiptV2) else receipt


def _booking_events(
    command: DlmmCommandV1,
    receipt: GatewayReceipt,
    position: ShadowPosition,
    now: datetime,
    price_impact_bps: int | None,
    *,
    exit_sale: Mapping[str, object] | None = None,
) -> tuple[AuditEvent, ...]:
    def event(kind: EventType, aggregate_id: UUID, payload: dict[str, object]) -> AuditEvent:
        return AuditEvent(
            event_id=uuid5(NAMESPACE_URL, f"event:{command.idempotency_key}:{kind.value}"),
            occurred_at=now,
            aggregate_id=aggregate_id,
            correlation_id=command.correlation_id,
            causation_id=command.command_id,
            schema_version=1,
            event_type=kind,
            payload=payload,
        )

    events = [
        event(
            EventType.COMMAND_CONFIRMED,
            command.command_id,
            {
                "mode": command.mode.value,
                "operation": command.operation.value,
                "idempotency_key": command.idempotency_key,
                "position_id": position.position_id,
                "signatures": list(receipt.signatures),
                # The quoted impact of the swap leg (None: an exit's quote was unavailable).
                "price_impact_bps": price_impact_bps,
            },
        )
    ]
    aggregate = position_aggregate_id(position.position_id)
    if command.operation is DlmmOperation.OPEN:
        events.append(
            event(
                EventType.POSITION_OPENED,
                aggregate,
                {"position_id": position.position_id, "mode": position.mode.value},
            )
        )
    elif position.status is PositionStatus.CLOSED:
        closed: dict[str, object] = {
            "position_id": position.position_id,
            "mode": position.mode.value,
        }
        if exit_sale is not None:
            # Closed in quote, or explicitly not: the token the exits returned is unsold.
            closed["exit_sale"] = dict(exit_sale)
        events.append(event(EventType.POSITION_CLOSED, aggregate, closed))
    return tuple(events)


async def execute_command(service: ExecutionService, command: DlmmCommand) -> SubmitResult:
    """Execute once; a block is definitive only while the durable record proves that
    nothing left (no receipt, not SUBMITTING/SUBMITTED/UNKNOWN)."""

    try:
        receipt = await service.execute(command.command_id)
    except ExecutionBlocked as error:
        try:
            state = await service.get_state(command.command_id)
        except Exception:
            return SubmitResult(SubmitStatus.PENDING, detail="EXECUTION_STATE_UNAVAILABLE")
        return settled_result(state, command, blocked=str(error))
    except Exception:
        return SubmitResult(SubmitStatus.PENDING, detail="SUBMIT_OUTCOME_UNKNOWN")
    return _receipt_result(receipt, command)


def settled_result(
    state: ExecutionState | None, command: DlmmCommand, *, blocked: str
) -> SubmitResult:
    if state is None:
        return SubmitResult(SubmitStatus.FAILED, detail=blocked)
    if state.receipt is not None:
        return _receipt_result(state.receipt, command)
    if state.phase in UNRESOLVED_PHASES:
        return SubmitResult(SubmitStatus.PENDING, detail=state.phase.value)
    return SubmitResult(SubmitStatus.FAILED, detail=blocked)


def _receipt_result(receipt: GatewayReceipt, command: DlmmCommand) -> SubmitResult:
    if receipt.state is GatewayState.CONFIRMED:
        # A V2 position exists only as the executor named it in the receipt.
        position = (
            receipt.position_address
            if isinstance(receipt, GatewayReceiptV2)
            else command.position_address
        )
        return SubmitResult(SubmitStatus.CONFIRMED, position_id=position)
    if receipt.state is GatewayState.FAILED:
        return SubmitResult(SubmitStatus.FAILED, detail="CHAIN_FAILED")
    return SubmitResult(SubmitStatus.PENDING, detail=receipt.state.value)


class GatewaySubmitter:
    """APPROVAL / LIVE through the T05 execution service and the internal gateway.

    APPROVAL stops after the simulated preview (an operator approval is the manual
    step of that stage; ``GatewayEntrySettler`` executes it once approved); LIVE
    submits only after the guard re-checks the durable state *after* simulation.
    ``record_position`` runs right after the simulation, before either: it receives
    the position the executor created for the OPEN and returns the context's entry
    price, which an immediate confirmation is booked at - as the settler and restart
    booking book it (``PositionContext.entry_price``), never the snapshot's price.
    """

    def __init__(
        self, service: ExecutionService, resolver: CommandResolver, book: GatewayFillBook
    ) -> None:
        self._service = service
        self._resolver = resolver
        self._book = book
        self._log = get_logger("worker.gateway")

    async def submit(
        self,
        command: DlmmCommand,
        market: PoolSnapshot,
        guard: SubmitGuard,
        record_position: PositionRecorder | None = None,
    ) -> SubmitResult:
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        self._resolver.stage(command)
        try:
            preview = await self._service.prepare(command.command_id)
        except ExecutionBlocked as error:
            return SubmitResult(SubmitStatus.FAILED, detail=str(error))
        finally:
            self._resolver.discard(command.command_id)
        entry_price = market.price
        if record_position is not None:
            simulation = preview.simulation
            position = (
                simulation.position_address
                if isinstance(simulation, SimulationResultV2)
                else command.position_address
            )
            if position is None:
                return SubmitResult(SubmitStatus.FAILED, detail="POSITION_NOT_ASSIGNED")
            try:
                entry_price = await record_position(position)
            except Exception as error:
                return SubmitResult(
                    SubmitStatus.BLOCKED, detail=getattr(error, "reason", "POSITION_NOT_RECORDED")
                )
        if preview.phase is ExecutionPhase.AWAITING_APPROVAL:
            return SubmitResult(SubmitStatus.PENDING, detail="AWAITING_APPROVAL")
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        result = await execute_command(self._service, command)
        if result.status is SubmitStatus.CONFIRMED:
            try:
                await self._book.book(command.command_id, entry_price)
            except Exception as error:
                # The fill is real: confirm it anyway, the position cycle books it later.
                self._log.error(
                    "gateway_fill_not_booked", command_id=str(command.command_id),
                    **error_fields(error),
                )
        return result


# --- entry executor ----------------------------------------------------------------------


class EntryStatus(StrEnum):
    OPENED = "OPENED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: EntryStatus
    plan_id: UUID
    reason: str | None = None
    position_id: str | None = None
    command_id: UUID | None = None


class EntryStore(ModeRepository, Protocol):
    """T04 entry contract plus the plan read the pre-fill guard needs."""

    def entry_plan(self, plan_id: UUID) -> PendingPlan | None: ...


class EntryBlocked(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WalletReader(Protocol):
    async def get_wallet_state(self, wallet: PublicKey) -> WalletState: ...


def entry_guard(modes: EntryStore, clock: Clock, mode: TradingMode, plan_id: UUID) -> SubmitGuard:
    """Re-read the durable state: the plan is still EXECUTING, no stop, same mode."""

    def guard() -> None:
        try:
            state = modes.mode_state()
            plan = modes.entry_plan(plan_id)
        except Exception as error:
            raise EntryBlocked("MODE_STATE_UNAVAILABLE") from error
        if plan is None or plan.status is not PendingPlanStatus.EXECUTING:
            raise EntryBlocked("PLAN_NOT_EXECUTING")
        today = clock.now().astimezone(UTC).date()
        if state.kill_switch_active:
            raise EntryBlocked("KILL_SWITCH_ACTIVE")
        if state.daily_stop_active_on(today):
            raise EntryBlocked("DAILY_STOP_REACHED")
        if state.mode is not mode:
            raise EntryBlocked("MODE_CHANGED")

    return guard


class EntryExecutor:
    def __init__(
        self,
        *,
        modes: EntryStore,
        audit: AuditLog,
        submitters: Mapping[TradingMode, CommandSubmitter],
        contexts: PositionContextStore,
        alerts: CriticalAlertReporter,
        clock: Clock,
        metrics: WorkerMetrics,
        policy: PipelinePolicy,
        wallet_address: str,
        anchors: AnchorSource = snapshot_anchor,
        pool_states: PoolStateReader | None = None,
        wallets: WalletReader | None = None,
        conservative_shadow: CommandSubmitter | None = None,
    ) -> None:
        """``pool_states``: the fresh active-bin read before a non-SHADOW entry (D07);
        ``wallets``: the wallet balances a non-SHADOW entry's token legs need (D08);
        ``conservative_shadow``: the SHADOW fill for an entry without measured depth."""

        self._modes = modes
        self._audit = audit
        self._submitters = dict(submitters)
        self._contexts = contexts
        self._alerts = alerts
        self._clock = clock
        self._metrics = metrics
        self._policy = policy
        self._wallet = wallet_address
        self._anchors = anchors
        self._pool_states = pool_states
        self._wallets = wallets
        self._conservative_shadow = conservative_shadow
        self._log = get_logger("worker.entries")

    async def execute(
        self,
        *,
        plan_id: UUID,
        decision_id: UUID,
        sized: SizedPlan,
        market: PoolSnapshot,
        correlation_id: UUID,
        causation_id: UUID | None,
        entry_short_volume: Decimal | None = None,
        conservative: bool = False,
    ) -> EntryResult:
        """``entry_short_volume``: the decision's current short window (it may come from
        the pool's volume history); ``conservative``: SHADOW fills with the conservative
        slippage model because no quote measured the depth (``DEPTH_UNAVAILABLE``)."""

        mode = self._modes.mode_state().mode
        submitter = self._submitters.get(mode)
        if mode is TradingMode.SHADOW and conservative and self._conservative_shadow is not None:
            submitter = self._conservative_shadow
        if submitter is None:
            self._cancel(plan_id)
            return self._finish(
                mode,
                EntryResult(
                    EntryStatus.NOT_CONFIGURED, plan_id, "EXECUTION_GATEWAY_NOT_CONFIGURED"
                ),
            )
        plan = sized.plan
        # Liquidity slippage (spec 6.8): a share of this plan's range, or the fixed bins.
        tolerance = self._policy.active_bin_tolerance_for(
            plan.lower_bin_offset, plan.upper_bin_offset
        )
        anchor: BinAnchor | None = None
        # Bins the fresh active bin moved from the snapshot's: the range the executor opens
        # (around the fresh bin) sits that far from the snapshot's price.
        drift = 0
        if mode is not TradingMode.SHADOW:
            planned = self._anchors(market)
            anchor = planned
            reason = "ENTRY_ANCHOR_UNAVAILABLE"
            if planned is not None:
                anchor, reason = await self._fresh_anchor(market, planned, tolerance)
            if anchor is None or planned is None:
                self._cancel(plan_id)
                return self._finish(mode, EntryResult(EntryStatus.BLOCKED, plan_id, reason))
            drift = anchor.active_bin_id - planned.active_bin_id
            refusal = fresh_high_refusal(plan, market, drift)
            if refusal is not None:
                self._log.info("entry_not_near_local_high", plan_id=str(plan_id), drift=drift)
                self._cancel(plan_id)
                return self._finish(mode, EntryResult(EntryStatus.BLOCKED, plan_id, refusal))
        try:
            self._modes.claim_entry(plan_id, self._clock.now())
        except ValueError as error:
            self._cancel(plan_id)
            return self._finish(
                mode, EntryResult(EntryStatus.BLOCKED, plan_id, _claim_reason(error))
            )

        try:
            command = build_entry_command(
                plan_id=plan_id,
                mode=mode,
                market=market,
                size_base_units=sized.size_base_units,
                strategy_kind=plan.strategy.value,
                lower_bin_offset=plan.lower_bin_offset,
                upper_bin_offset=plan.upper_bin_offset,
                expires_at=plan.expires_at,
                correlation_id=str(correlation_id),
                ruleset_version=plan.ruleset_version,
                wallet_address=self._wallet,
                max_slippage_bps=self._policy.max_slippage_bps,
                anchor=anchor,
                active_bin_tolerance=tolerance,
            )
        except EntryCommandUnavailable as unavailable:
            self._fail(plan_id)
            return self._finish(
                mode, EntryResult(EntryStatus.BLOCKED, plan_id, unavailable.reason)
            )
        if mode is not TradingMode.SHADOW:
            shortfall = await self._wallet_shortfall(command, market)
            if shortfall is not None:
                return await self._blocked(mode, plan_id, command, shortfall, correlation_id)
        guard = entry_guard(self._modes, self._clock, mode, plan_id)
        try:
            guard()
        except EntryBlocked as blocked:
            return await self._blocked(mode, plan_id, command, blocked.reason, correlation_id)

        try:
            if mode is TradingMode.SHADOW:
                await self._contexts.put(
                    _position_context(
                        plan_id, decision_id, sized, market, mode, command,
                        position_id=entry_position_address(plan_id),
                        entry_short_volume=entry_short_volume,
                    )
                )
            await self._audit.append(
                audit_event(
                    EventType.COMMAND_SUBMITTED,
                    self._clock.now(),
                    command.command_id,
                    correlation_id,
                    causation_id,
                    {
                        "stage": "authorized",
                        "plan_id": str(plan_id),
                        "mode": mode.value,
                        "command_digest": canonical_command_digest(command),
                        "idempotency_key": command.idempotency_key,
                        "size_base_units": sized.size_base_units,
                    },
                )
            )
        except Exception as error:
            self._fail(plan_id)
            self._log.critical("audit_persistence_failed", **error_fields(error))
            self._alerts.raise_alert(
                "AUDIT_PERSISTENCE_FAILED",
                "audit record could not be persisted; money execution was blocked",
            )
            return self._finish(
                mode, EntryResult(EntryStatus.BLOCKED, plan_id, "AUDIT_PERSISTENCE_FAILED")
            )

        try:
            if mode is TradingMode.SHADOW:
                result = await submitter.submit(command, market, guard)
            else:
                recorder = self._position_recorder(
                    plan_id, decision_id, sized, market, command, entry_short_volume, drift
                )
                result = await submitter.submit(command, market, guard, recorder)
        except Exception as error:
            # The outcome is unknown: keep the plan EXECUTING (capacity stays reserved)
            # so restart recovery reconciles it; never retry the submit here.
            self._log.error("submit_outcome_unknown", plan_id=str(plan_id), **error_fields(error))
            result = SubmitResult(SubmitStatus.PENDING, detail="SUBMIT_OUTCOME_UNKNOWN")
        if result.status is SubmitStatus.CONFIRMED and result.position_id is not None:
            return self._finish(mode, self._confirm(plan_id, result.position_id, command))
        if result.status is SubmitStatus.PENDING:
            return self._finish(
                mode,
                EntryResult(
                    EntryStatus.PENDING, plan_id, result.detail, command_id=command.command_id
                ),
            )
        if result.status is SubmitStatus.BLOCKED:
            return await self._blocked(
                mode, plan_id, command, result.detail or "BLOCKED", correlation_id
            )
        self._fail(plan_id)
        return self._finish(
            mode,
            EntryResult(EntryStatus.FAILED, plan_id, result.detail, command_id=command.command_id),
        )

    def _confirm(self, plan_id: UUID, position_id: str, command: DlmmCommand) -> EntryResult:
        return confirm_filled_entry(
            self._modes, self._alerts, plan_id, position_id, command.command_id
        )

    def _position_recorder(
        self,
        plan_id: UUID,
        decision_id: UUID,
        sized: SizedPlan,
        market: PoolSnapshot,
        command: DlmmCommand,
        entry_short_volume: Decimal | None,
        drift: int,
    ) -> PositionRecorder:
        """The context of a gateway entry, under the position the executor created; its
        range follows the active bin the command was anchored on (``drift`` bins from the
        snapshot's)."""

        async def record(position_id: str) -> Decimal:
            context = _position_context(
                plan_id, decision_id, sized, market, command.mode, command,
                position_id=position_id,
                entry_short_volume=entry_short_volume,
                drift=drift,
            )
            try:
                await self._contexts.put(context)
            except Exception as error:
                self._log.critical("audit_persistence_failed", **error_fields(error))
                self._alerts.raise_alert(
                    "AUDIT_PERSISTENCE_FAILED",
                    "position context could not be persisted; money execution was blocked",
                )
                raise EntryBlocked("AUDIT_PERSISTENCE_FAILED") from error
            return context.entry_price

        return record

    async def _fresh_anchor(
        self, market: PoolSnapshot, anchor: BinAnchor, tolerance: int
    ) -> tuple[BinAnchor | None, str]:
        """Re-read the LB pair: the sweep's active bin is minutes old by now (D07).

        The fresh bin becomes the anchor when it is within ``tolerance`` bins of the planned
        one (the plan's liquidity slippage); a larger move, another pair or no read at all
        cancels the entry.
        """

        reader = self._pool_states
        if reader is None:
            return None, "ACTIVE_BIN_UNAVAILABLE"
        try:
            states = await reader.get_pool_states([market.pool])
        except Exception as error:
            self._log.warning("active_bin_read_failed", **error_fields(error))
            return None, "ACTIVE_BIN_UNAVAILABLE"
        state = states.get(market.pool.address.root)
        if state is None:
            return None, "ACTIVE_BIN_UNAVAILABLE"
        if (
            state.token_x_mint != market.pool.base_mint.root
            or state.token_y_mint != market.pool.quote_mint.root
            or state.bin_step != market.bin_step
        ):
            return None, "POOL_STATE_MISMATCH"
        if abs(state.active_bin_id - anchor.active_bin_id) > tolerance:
            self._log.info(
                "active_bin_drift",
                planned=anchor.active_bin_id,
                current=state.active_bin_id,
                tolerance=tolerance,
            )
            return None, "ACTIVE_BIN_DRIFT"
        return replace(anchor, active_bin_id=state.active_bin_id), ""

    async def _wallet_shortfall(self, command: DlmmCommand, market: PoolSnapshot) -> str | None:
        """The leg the wallet cannot fund; the executor does not swap (D08)."""

        reader = self._wallets
        if reader is None:
            return "WALLET_STATE_UNAVAILABLE"
        try:
            state = await reader.get_wallet_state(PublicKey(self._wallet))
        except Exception as error:
            self._log.warning("wallet_state_unavailable", **error_fields(error))
            return "WALLET_STATE_UNAVAILABLE"
        legs = (
            (market.pool.base_mint.root, command.amount_x, "WALLET_INSUFFICIENT_BASE_TOKEN"),
            (market.pool.quote_mint.root, command.amount_y, "WALLET_INSUFFICIENT_QUOTE_TOKEN"),
        )
        for mint, amount, reason in legs:
            needed = int(amount or "0")
            if needed > 0 and needed > self._holding(state, mint):
                return reason
        return None

    def _holding(self, state: WalletState, mint: str) -> int:
        balance = state.token_balances.get(mint)
        held = 0 if balance is None else balance.root
        if mint == WRAPPED_SOL_MINT:
            # The SDK wraps native SOL; keep a reserve for fees and rent.
            native = state.native_balance.root
            held += max(native - self._policy.wallet_native_reserve_lamports, 0)
        return held

    async def _blocked(
        self,
        mode: TradingMode,
        plan_id: UUID,
        command: DlmmCommand,
        reason: str,
        correlation_id: UUID,
    ) -> EntryResult:
        self._fail(plan_id)
        try:
            await self._audit.append(
                audit_event(
                    EventType.RISK_REJECTED,
                    self._clock.now(),
                    command.command_id,
                    correlation_id,
                    None,
                    {"stage": "execution", "plan_id": str(plan_id), "reason": reason},
                )
            )
        except Exception as error:
            self._log.error("blocked_entry_not_audited", **error_fields(error))
        return self._finish(
            mode, EntryResult(EntryStatus.BLOCKED, plan_id, reason, command_id=command.command_id)
        )

    def _cancel(self, plan_id: UUID) -> None:
        try:
            self._modes.cancel_plan(plan_id)
        except (ValueError, OSError) as error:
            self._log.warning("plan_cancel_skipped", plan_id=str(plan_id), **error_fields(error))

    def _fail(self, plan_id: UUID) -> None:
        fail_entry_plan(self._modes, plan_id)

    def _finish(self, mode: TradingMode, result: EntryResult) -> EntryResult:
        self._metrics.execution(mode, result.status.value)
        self._log.info(
            "entry_finished",
            plan_id=str(result.plan_id),
            status=result.status.value,
            reason=result.reason,
        )
        return result


def confirm_filled_entry(
    modes: ModeRepository,
    alerts: CriticalAlertReporter,
    plan_id: UUID,
    position_id: str,
    command_id: UUID,
) -> EntryResult:
    """EXECUTING -> OPENED; a fill the bookkeeping refuses is never hidden."""

    try:
        modes.confirm_entry(plan_id, position_id)
    except ValueError as error:
        get_logger("worker.entries").critical(
            "fill_not_tracked", plan_id=str(plan_id), **error_fields(error)
        )
        alerts.raise_alert(
            "ENTRY_FILL_UNTRACKED",
            f"position {position_id} was filled but plan {plan_id} "
            "could not be confirmed; reconcile before trading continues",
            dedupe_key=f"untracked:{plan_id}",
        )
        return EntryResult(
            EntryStatus.FAILED,
            plan_id,
            "ENTRY_FILL_UNTRACKED",
            position_id=position_id,
            command_id=command_id,
        )
    return EntryResult(
        EntryStatus.OPENED, plan_id, position_id=position_id, command_id=command_id
    )


def fail_entry_plan(modes: ModeRepository, plan_id: UUID) -> None:
    try:
        modes.fail_entry(plan_id)
    except Exception as error:
        # Left EXECUTING: it keeps its capacity reserved until recovery settles it.
        get_logger("worker.entries").error(
            "entry_fail_not_recorded", plan_id=str(plan_id), **error_fields(error)
        )


_NOT_NEAR_LOCAL_HIGH = StrategyRejection.NOT_NEAR_LOCAL_HIGH.value


def fresh_high_refusal(plan: StrategyPlan, market: PoolSnapshot, drift: int) -> str | None:
    """``NOT_NEAR_LOCAL_HIGH`` when a Bid-Ask "from the high" no longer is (spec 6.8, No 31).

    The planner checked the rule on the sweep's price; a non-SHADOW entry opens around the
    freshly read active bin, ``drift`` bins away, whose price is the sweep's moved as many
    bin steps - within the liquidity slippage that may be a third of the range below the
    plan. The rule is re-checked there with the high and the fraction the plan recorded in
    its evidence (``local_high_price``, the threshold of ``price_to_local_high``); without
    them nothing proves the entry is from the high, so it is refused. Other plans: ``None``.
    """

    if plan.selection_reason is not StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH:
        return None
    evidence = {item.name: item for item in plan.selection_evidence}
    high_item = evidence.get("local_high_price")
    ratio_item = evidence.get("price_to_local_high")
    high = _evidence_decimal(None if high_item is None else high_item.value)
    fraction = _evidence_decimal(None if ratio_item is None else ratio_item.threshold)
    if high is None or fraction is None or high <= 0 or market.bin_step <= 0:
        return _NOT_NEAR_LOCAL_HIGH
    step = Decimal(1) + Decimal(market.bin_step) / _BPS
    return None if market.price * step**drift >= high * fraction else _NOT_NEAR_LOCAL_HIGH


def _evidence_decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        value = Decimal(text)
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _claim_reason(error: ValueError) -> str:
    message = str(error)
    if "kill switch" in message:
        return "KILL_SWITCH_ACTIVE"
    if "daily stop" in message:
        return "DAILY_STOP_OR_RISK_DAY"
    if "expired" in message:
        return "PLAN_EXPIRED"
    return "CLAIM_REJECTED"


def _position_context(
    plan_id: UUID,
    decision_id: UUID,
    sized: SizedPlan,
    market: PoolSnapshot,
    mode: TradingMode,
    command: DlmmCommand,
    *,
    position_id: str,
    entry_short_volume: Decimal | None = None,
    drift: int = 0,
) -> PositionContext:
    """``drift``: bins between the active bin the command was anchored on and the snapshot's
    (a non-SHADOW entry re-reads it, D07); the entry is that bin's price, the snapshot price
    moved by as many bin steps, and the range is the plan's bins around it."""

    plan = sized.plan
    step = Decimal(1) + Decimal(market.bin_step) / _BPS
    short = market.windows.get("5m")
    long = market.windows.get("30m")
    fees = long.fees if long is not None and long.fees is not None else Decimal(0)
    if entry_short_volume is None:
        entry_short_volume = short.volume if short is not None else Decimal(0)
    if command.min_bin_id is None or command.max_bin_id is None:  # pragma: no cover
        raise ValueError("an entry command always carries its bin range")
    entry_price = market.price * step**drift
    return PositionContext(
        position_id=position_id,
        plan_id=plan_id,
        decision_id=decision_id,
        pool_address=market.pool.address.root,
        base_mint=market.pool.base_mint.root,
        entry_price=entry_price,
        lower_price=entry_price * step**plan.lower_bin_offset,
        upper_price=entry_price * step**plan.upper_bin_offset,
        entry_short_volume=entry_short_volume,
        baseline_fee_tvl=fees / market.tvl if market.tvl > 0 else Decimal(0),
        entry_tvl=market.tvl,
        maximum_holding_period=plan.exit_policy.maximum_holding_period,
        opened_at=market.observed_at,
        mode=mode,
        ruleset_version=plan.ruleset_version,
        correlation_id=str(command.correlation_id),
        wallet_address=command.wallet_address,
        max_slippage_bps=command.max_slippage_bps,
        min_bin_id=command.min_bin_id,
        max_bin_id=command.max_bin_id,
    )


def audit_event(
    kind: EventType,
    occurred_at: datetime,
    aggregate_id: UUID,
    correlation_id: UUID,
    causation_id: UUID | None,
    payload: dict[str, object],
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid4(),
        occurred_at=occurred_at,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        schema_version=1,
        event_type=kind,
        payload=payload,
    )


# --- durable continuation of gateway entries ----------------------------------------------


class GatewayEntryStore(EntryStore, Protocol):
    def entry_plans(self) -> tuple[PendingPlan, ...]: ...


class GatewayEntrySettler:
    """Continue every EXECUTING non-SHADOW entry from its durable execution record.

    * APPROVED (the operator's one-shot approval) or SIMULATED (a LIVE worker died
      between simulation and submit) -> guard -> ``execute`` exactly once;
    * SUBMITTING / SUBMITTED / UNKNOWN -> reconcile with the gateway, never resend;
    * CONFIRMED -> book the fill and confirm the entry.

    AWAITING_APPROVAL waits for the operator; recovery fails it once it expires.
    Runs inside the entry job, under the same lease as new entries and recovery's
    settlement, so no second executor works on the same plan.
    """

    def __init__(
        self,
        *,
        modes: GatewayEntryStore,
        executions: ExecutionStore,
        service: ExecutionService,
        book: GatewayFillBook,
        contexts: PositionContextStore,
        alerts: CriticalAlertReporter,
        clock: Clock,
        metrics: WorkerMetrics,
    ) -> None:
        self._modes = modes
        self._executions = executions
        self._service = service
        self._book = book
        self._contexts = contexts
        self._alerts = alerts
        self._clock = clock
        self._metrics = metrics
        self._log = get_logger("worker.entries")

    async def run(self) -> tuple[EntryResult, ...]:
        results: list[EntryResult] = []
        for plan in self._modes.entry_plans():
            if plan.status is not PendingPlanStatus.EXECUTING:
                continue
            record = await self._executions.get_by_idempotency_key(
                entry_idempotency_key(plan.plan_id)
            )
            if record is None or record.command.mode is TradingMode.SHADOW:
                continue
            result = await self._advance(plan, record)
            if result is not None:
                self._metrics.execution(record.command.mode, result.status.value)
                self._log.info(
                    "gateway_entry_continued",
                    plan_id=str(plan.plan_id),
                    status=result.status.value,
                    reason=result.reason,
                )
                results.append(result)
        return tuple(results)

    async def _advance(self, plan: PendingPlan, record: ExecutionRecord) -> EntryResult | None:
        command = record.command
        if record.phase in _EXECUTABLE_PHASES:
            try:
                entry_guard(self._modes, self._clock, command.mode, plan.plan_id)()
            except EntryBlocked as blocked:
                fail_entry_plan(self._modes, plan.plan_id)
                return EntryResult(
                    EntryStatus.BLOCKED,
                    plan.plan_id,
                    blocked.reason,
                    command_id=command.command_id,
                )
            outcome = await execute_command(self._service, command)
        elif record.phase in UNRESOLVED_PHASES:
            try:
                state = await self._service.reconcile(command.command_id)
            except Exception as error:
                self._log.warning(
                    "gateway_entry_reconcile_failed",
                    plan_id=str(plan.plan_id),
                    **error_fields(error),
                )
                return None
            outcome = settled_result(state, command, blocked=record.phase.value)
        elif record.phase is ExecutionPhase.CONFIRMED:
            # Possibly reconciled elsewhere (the recovery job): the receipt names the position.
            outcome = SubmitResult(SubmitStatus.CONFIRMED, position_id=record.position_address)
        else:
            return None
        return await self._settle(plan, command, outcome)

    async def _settle(
        self, plan: PendingPlan, command: DlmmCommand, outcome: SubmitResult
    ) -> EntryResult:
        if outcome.status is SubmitStatus.CONFIRMED and outcome.position_id is None:
            # Landed on chain but unnamed: never fail it (the position would be orphaned).
            self._alerts.raise_alert(
                "ENTRY_POSITION_UNKNOWN",
                f"entry of plan {plan.plan_id} is confirmed but its position is not known",
                dedupe_key=f"position-unknown:{plan.plan_id}",
            )
            return EntryResult(
                EntryStatus.PENDING, plan.plan_id, "POSITION_UNKNOWN", command_id=command.command_id
            )
        if outcome.status is SubmitStatus.CONFIRMED and outcome.position_id is not None:
            context = await self._contexts.get(outcome.position_id)
            if context is not None:
                try:
                    await self._book.book(command.command_id, context.entry_price)
                except Exception as error:
                    self._log.error(
                        "gateway_fill_not_booked",
                        plan_id=str(plan.plan_id),
                        **error_fields(error),
                    )
            return confirm_filled_entry(
                self._modes, self._alerts, plan.plan_id, outcome.position_id, command.command_id
            )
        if outcome.status is SubmitStatus.PENDING:
            return EntryResult(
                EntryStatus.PENDING, plan.plan_id, outcome.detail, command_id=command.command_id
            )
        fail_entry_plan(self._modes, plan.plan_id)
        return EntryResult(
            EntryStatus.FAILED, plan.plan_id, outcome.detail, command_id=command.command_id
        )


# --- non-SHADOW positions through the gateway ---------------------------------------------


class PositionActionStatus(StrEnum):
    EXITED = "EXITED"
    REDUCED = "REDUCED"
    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    HELD = "HELD"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    # The exit went through; the base token it returned is being sold (G22).
    SELLING = "SELLING"


class SaleStatus(StrEnum):
    """Where the sale of the base token one confirmed exit returned stands (G22)."""

    DISABLED = "DISABLED"  # SWAP_AFTER_EXIT is off: the token stays in the wallet
    NOT_REQUIRED = "NOT_REQUIRED"  # the exit returned no base token
    PENDING = "PENDING"  # waiting for the chain settlement, an unknown outcome or the gateway
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SELLING = "SELLING"  # a part went out or failed; the next one follows
    SOLD = "SOLD"
    UNSOLD = "UNSOLD"  # the token stays under a critical alert; the operator decides


_TERMINAL_SALES = frozenset(
    {SaleStatus.DISABLED, SaleStatus.NOT_REQUIRED, SaleStatus.SOLD, SaleStatus.UNSOLD}
)
_SALE_ACTION_STATUS = {
    SaleStatus.PENDING: PositionActionStatus.PENDING,
    SaleStatus.AWAITING_APPROVAL: PositionActionStatus.AWAITING_APPROVAL,
    SaleStatus.SELLING: PositionActionStatus.SELLING,
}
_PRICE_IMPACT_BREACH = "PRICE_IMPACT_BREACH"
# Refusals of a part that a smaller part may pass: the quote's impact above the limit, or a
# pool without liquidity for the whole part (the official SDK's quote error, executor sdk.ts).
_SPLIT_CODES = frozenset({_PRICE_IMPACT_BREACH, "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY"})


@dataclass(frozen=True, slots=True)
class SaleProgress:
    """The sale of one exit's base token, derived from durable records every cycle."""

    status: SaleStatus
    returned: int = 0
    sold: int = 0
    parts: tuple[ExecutionRecord, ...] = field(default=())
    detail: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_SALES

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "returned_base_units": str(self.returned),
            "sold_base_units": str(self.sold),
            "unsold_base_units": str(max(self.returned - self.sold, 0)),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _SalePlan:
    sold: int
    confirmed: tuple[ExecutionRecord, ...]
    verdict: SaleStatus | None = None
    detail: str | None = None
    active: "_Attempt | None" = None
    renew: "_Attempt | None" = None
    next_part: int = 1
    next_size: int = 0


def sale_part_limit(settings: SwapSettings) -> int:
    """The most sale commands one exit's sale may take: every halving after every sold part,
    plus the failed attempts; past it the sale ends unsold (alert)."""

    return (1 << settings.max_splits) * (settings.max_splits + 1) + settings.attempts


def _sold_by(record: ExecutionRecord) -> int:
    """Base units a confirmed sale part sold: the chain settlement, else its exact-in amount."""

    receipt = record.receipt
    settlement = receipt.settlement if isinstance(receipt, GatewayReceiptV2) else None
    if settlement is not None and int(settlement.delta_x) < 0:
        return -int(settlement.delta_x)
    return int(getattr(record.command, "in_amount", None) or 0)


def _breached(record: ExecutionRecord) -> bool:
    """The part was refused for its quoted impact or the pool's liquidity (nothing was
    sent): sell a smaller one."""

    if record.receipt is not None:
        return False
    errors = record.simulation.errors
    if errors:
        return errors[0].code in _SPLIT_CODES
    return swap_rejection(record.command, record.simulation) in _SPLIT_CODES


def _plan_sale(
    parts: Sequence["_Attempt"], returned: int, settings: SwapSettings, now: datetime
) -> _SalePlan:
    """The next step of a sale from its parts so far; pure, so a restart decides the same.

    Only the latest part can be live. A part refused for its impact is retried at half
    its size down to ``returned / 2**max_splits``; below that the token stays (UNSOLD).
    After a sold part the next one is at most as large; a dust remainder smaller than the
    smallest part rides with the last one. ``attempts`` failed parts end the sale."""

    smallest = max(-(-returned // (1 << settings.max_splits)), 1)
    most_parts = sale_part_limit(settings)
    sold, failures = 0, 0
    confirmed: list[ExecutionRecord] = []
    last: tuple[str, int] | None = None
    for item in parts:
        record = item.record
        size = int(getattr(record.command, "in_amount", None) or 0)
        if record.phase is ExecutionPhase.CONFIRMED:
            sold += _sold_by(record)
            confirmed.append(record)
            last = ("sold", size)
        elif record.phase in UNRESOLVED_PHASES | _EXECUTABLE_PHASES or (
            record.phase is ExecutionPhase.AWAITING_APPROVAL and record.command.expires_at > now
        ):
            return _SalePlan(sold, tuple(confirmed), active=item)
        elif _expired_unapproved(record, now):
            return _SalePlan(sold, tuple(confirmed), renew=item)
        elif _breached(record):
            last = ("breach", size)
        else:
            failures += 1
            last = ("failed", size)
    remaining = returned - sold
    if remaining <= 0:
        return _SalePlan(sold, tuple(confirmed), verdict=SaleStatus.SOLD)
    if failures >= settings.attempts or len(parts) >= most_parts:
        return _SalePlan(
            sold, tuple(confirmed), verdict=SaleStatus.UNSOLD, detail="ATTEMPTS_EXHAUSTED"
        )
    if last is None:
        size = remaining
    elif last[0] == "breach":
        size = last[1] // 2
        if size < smallest:
            return _SalePlan(
                sold, tuple(confirmed), verdict=SaleStatus.UNSOLD, detail="PRICE_IMPACT_EXCEEDED"
            )
    else:
        size = min(remaining, last[1])
        if remaining - size < smallest:
            size = remaining
    return _SalePlan(
        sold, tuple(confirmed), next_part=len(parts) + 1, next_size=min(size, remaining)
    )


@dataclass(frozen=True, slots=True)
class PositionActionResult:
    position_id: str
    status: PositionActionStatus
    attempt: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One started attempt of a position's sequence and its latest preview."""

    number: int
    preview: int
    record: ExecutionRecord


# Conditions of a sale that its next confirmed part has cleared: the gateway answered and
# sold. Never the verdicts that need a person (unsold, failed attempts, unknown amount).
_CLEARED_BY_SALE_PART = ("EXIT_SALE_GATEWAY_UNAVAILABLE",)
# Conditions a later successful gateway action of the same position has cleared; a CLOSE is
# booked only once every sale of the position has ended, so the sales' transient
# conditions are cleared with it.
_CLEARED_BY_GATEWAY_SUCCESS = (
    "POSITION_ACTION_FAILED",
    "EXIT_GATEWAY_UNAVAILABLE",
    "EXIT_REQUIRES_EXECUTION_GATEWAY",
    *_CLEARED_BY_SALE_PART,
    # Only while a part was live or could not be recorded; the CLOSE books after both ended.
    "EXIT_SALE_OUTCOME_UNKNOWN",
    "EXIT_SALE_BLOCKED",
)


class GatewayPositionManager:
    """Exit (CLOSE) and reduce (REMOVE) non-SHADOW positions through the gateway.

    Each position has one durable sequence of at most ``attempts`` attempts keyed
    ``position-action:{position_id}:{n}``. Only the latest attempt is ever live: an
    unresolved one is reconciled (never resent), an approved one is executed once,
    and a new attempt - always with a fresh simulation - starts only after the
    previous one provably ended. An APPROVAL preview that expired before anybody
    approved it sent nothing and spends no attempt: the same attempt gets a renewed
    preview (``position-action:{position_id}:{n}:{k}``). A reduction happens at most
    once per position. Without a reachable gateway the position stays open under a
    critical alert, which the next successful action of the position closes.

    The sale after an exit (G22): the base token a confirmed REMOVE/CLOSE returned (its
    receipt's settlement) is sold to the quote token by SWAP commands
    ``exit-sale:{position_id}:{attempt}:{part}`` - one live part at a time, a refused
    part retried at half size, an unknown outcome only reconciled, an expired
    unapproved card renewed (``:{k}``) - under any kill-switch policy. A reduction's sale
    never holds an exit; the CLOSE is booked once every sale of the position has ended,
    with their receipts, and marked sold or unsold.
    """

    def __init__(
        self,
        *,
        modes: ModeRepository,
        accounting: AccountingRepository,
        contexts: PositionContextStore,
        history: SnapshotHistory,
        executions: ExecutionStore,
        service: ExecutionService,
        resolver: CommandResolver,
        book: GatewayFillBook,
        alerts: CriticalAlertReporter,
        cleared: SystemAlertResolver,
        clock: Clock,
        metrics: WorkerMetrics,
        attempts: int = POSITION_ACTION_ATTEMPTS,
        reduce_bps: int = REDUCE_BPS,
        swap: SwapSettings | None = None,
    ) -> None:
        """``cleared`` closes this manager's own alerts once the position's action went
        through; the kill-switch policy is read from the durable mode state. ``swap``:
        the sale after an exit, which the worker's runtime reads with its other settings
        (``SWAP_*``); without it the defaults - the sale on."""

        if attempts <= 0 or not 0 < reduce_bps < 10_000:
            raise ValueError("attempts must be positive and reduce_bps inside (0, 10000)")
        self._swap = swap if swap is not None else SwapSettings()
        self._modes = modes
        self._accounting = accounting
        self._contexts = contexts
        self._history = history
        self._executions = executions
        self._service = service
        self._resolver = resolver
        self._book = book
        self._alerts = alerts
        self._cleared = cleared
        self._clock = clock
        self._metrics = metrics
        self._attempts = attempts
        self._reduce_bps = reduce_bps
        self._log = get_logger("worker.positions.gateway")

    async def book_confirmed_entries(self) -> None:
        """Book entries confirmed outside this worker's settlement (restart recovery)."""

        for position_id in self._modes.open_positions():
            if self._accounting.position_for(position_id) is not None:
                continue
            context = await self._contexts.get(position_id)
            if context is None or context.mode is TradingMode.SHADOW:
                continue
            record = await self._executions.get_by_idempotency_key(
                entry_idempotency_key(context.plan_id)
            )
            if record is None or record.phase is not ExecutionPhase.CONFIRMED:
                continue
            try:
                await self._book.book(record.command.command_id, context.entry_price)
            except Exception as error:
                self._log.error(
                    "gateway_fill_not_booked", position_id=position_id, **error_fields(error)
                )
                self._alerts.raise_alert(
                    "GATEWAY_FILL_UNBOOKED",
                    f"confirmed entry of position {position_id} could not be booked "
                    + alert_subject("position", position_id),
                    dedupe_key=f"unbooked:{position_id}",
                )

    async def apply(
        self, actions: Mapping[str, PositionActionKind]
    ) -> tuple[PositionActionResult, ...]:
        """``actions``: this cycle's monitor decision per position id."""

        results: list[PositionActionResult] = []
        for position_id in self._modes.open_positions():
            position = self._accounting.position_for(position_id)
            if (
                position is None
                or position.mode is TradingMode.SHADOW
                or position.status is not PositionStatus.OPEN
            ):
                continue
            result = await self._advance(position, actions.get(position_id))
            if result is not None:
                self._metrics.execution(position.mode, f"POSITION_{result.status.value}")
                self._log.info(
                    "gateway_position_action",
                    position_id=position_id,
                    status=result.status.value,
                    attempt=result.attempt,
                    detail=result.detail,
                )
                results.append(result)
        return tuple(results)

    async def _advance(
        self, position: ShadowPosition, action: PositionActionKind | None
    ) -> PositionActionResult | None:
        position_id = position.position_id
        history = await self._sequence(position_id)
        for item in history:
            if _partially_executed(item.record):
                return self._operator_required(position_id, item.record, item.number)
        # A reduction's sale runs alongside and never holds the way out (spec 8.5, D08).
        for item in history:
            if self._booked_reduction(item):
                await self._drive_sale(position, item, drive=True)
        latest = history[-1] if history else None
        if latest is not None:
            result = await self._continue(position, latest.record, latest.number)
            if result is not None:
                return result
        if action not in (PositionActionKind.EXIT, PositionActionKind.REDUCE):
            return None
        operation = DlmmOperation.CLOSE
        if action is PositionActionKind.REDUCE:
            if any(
                item.record.command.operation is DlmmOperation.REMOVE
                and item.record.phase is ExecutionPhase.CONFIRMED
                for item in history
            ):
                return None
            operation = DlmmOperation.REMOVE
        if latest is not None and _expired_unapproved(latest.record, self._clock.now()):
            # Nobody approved that preview and nothing was sent: the same attempt, renewed.
            attempt, preview = latest.number, latest.preview + 1
        else:
            attempt, preview = len(history) + 1, 1
        if attempt > self._attempts:
            self._alerts.raise_alert(
                "POSITION_ACTION_ATTEMPTS_EXHAUSTED",
                f"position {position_id} needs an operator: {self._attempts} gateway "
                "attempts ended without an exit " + alert_subject("position", position_id),
                dedupe_key=f"exhausted:{position_id}",
            )
            return PositionActionResult(position_id, PositionActionStatus.EXHAUSTED)
        policy = self._modes.mode_state().active_kill_switch_policy
        if policy is KillSwitchPolicy.HOLD_AND_ALERT:
            return PositionActionResult(position_id, PositionActionStatus.HELD, attempt)
        return await self._start(position, operation, attempt, preview)

    async def _continue(
        self, position: ShadowPosition, record: ExecutionRecord, attempt: int
    ) -> PositionActionResult | None:
        """Drive the latest attempt; ``None`` means it has ended and frees the slot."""

        position_id = position.position_id
        if record.phase in UNRESOLVED_PHASES:
            try:
                state = await self._service.reconcile(record.command.command_id)
            except Exception as error:
                return self._unavailable(position_id, attempt, error)
            outcome = settled_result(state, record.command, blocked=record.phase.value)
            return await self._outcome(position, record.command, outcome, attempt)
        if record.phase in _EXECUTABLE_PHASES:
            outcome = await execute_command(self._service, record.command)
            return await self._outcome(position, record.command, outcome, attempt)
        if record.phase is ExecutionPhase.AWAITING_APPROVAL:
            if record.command.expires_at > self._clock.now():
                return PositionActionResult(
                    position_id, PositionActionStatus.AWAITING_APPROVAL, attempt
                )
            return None
        if record.phase is ExecutionPhase.CONFIRMED:
            settled = await self._settle(position, record.command, attempt)
            if settled.status is PositionActionStatus.REDUCED:
                return None  # the reduction is booked: the slot is free for an exit
            return settled
        return None  # FAILED / EXPIRED: the attempt ended without a transaction

    async def _start(
        self, position: ShadowPosition, operation: DlmmOperation, attempt: int, preview: int
    ) -> PositionActionResult:
        position_id = position.position_id
        context = await self._contexts.get(position_id)
        if context is None:
            self._alerts.raise_alert(
                "POSITION_CONTEXT_MISSING",
                f"open position {position_id} has no durable context "
                + alert_subject("position", position_id),
                dedupe_key=f"context:{position_id}",
            )
            return PositionActionResult(
                position_id, PositionActionStatus.FAILED, attempt, "CONTEXT_MISSING"
            )
        command = DlmmCommandV2(
            command_id=position_action_command_id(position_id, attempt, preview),
            idempotency_key=position_action_key(position_id, attempt, preview),
            correlation_id=_correlation(context.correlation_id),
            ruleset_version=position.ruleset_version,
            mode=position.mode,
            operation=operation,
            pool_address=position.pool_address,
            wallet_address=context.wallet_address,
            position_address=position_id,
            expires_at=self._clock.now() + POSITION_ACTION_TTL,
            max_slippage_bps=context.max_slippage_bps,
            min_bin_id=context.min_bin_id,
            max_bin_id=context.max_bin_id,
            remove_bps=10_000 if operation is DlmmOperation.CLOSE else self._reduce_bps,
        )
        self._resolver.stage(command)
        try:
            prepared = await self._service.prepare(command.command_id)
        except GatewayRejected as error:
            return self._unavailable(position_id, attempt, error)
        except ExecutionBlocked as error:
            # A failed or too-expensive simulation: this attempt ended, nothing was sent.
            failed = SubmitResult(SubmitStatus.FAILED, detail=str(error))
            return await self._outcome(position, command, failed, attempt)
        except Exception as error:
            return self._unavailable(position_id, attempt, error)
        finally:
            self._resolver.discard(command.command_id)
        if prepared.phase is ExecutionPhase.AWAITING_APPROVAL:
            return PositionActionResult(
                position_id, PositionActionStatus.AWAITING_APPROVAL, attempt
            )
        outcome = await execute_command(self._service, command)
        return await self._outcome(position, command, outcome, attempt)

    async def _outcome(
        self,
        position: ShadowPosition,
        command: DlmmCommand,
        outcome: SubmitResult,
        attempt: int,
    ) -> PositionActionResult:
        position_id = position.position_id
        if outcome.status is SubmitStatus.CONFIRMED:
            return await self._settle(position, command, attempt)
        if outcome.status is SubmitStatus.PENDING:
            return PositionActionResult(
                position_id, PositionActionStatus.PENDING, attempt, outcome.detail
            )
        try:
            record = await self._executions.get(command.command_id)
        except Exception as error:
            # The next cycle re-reads the sequence before any new attempt.
            self._log.error("execution_record_unavailable", **error_fields(error))
            record = None
        if record is not None and _partially_executed(record):
            return self._operator_required(position_id, record, attempt)
        self._alerts.raise_alert(
            "POSITION_ACTION_FAILED",
            f"gateway {command.operation.value} of position {position_id} failed "
            f"(attempt {attempt}): {outcome.detail} " + alert_subject("position", position_id),
            dedupe_key=f"action-failed:{position_id}:{attempt}",
        )
        return PositionActionResult(
            position_id, PositionActionStatus.FAILED, attempt, outcome.detail
        )

    async def _settle(
        self, position: ShadowPosition, command: DlmmCommand, attempt: int
    ) -> PositionActionResult:
        position_id = position.position_id
        market = await self._history.history(
            position.pool_address, until=self._clock.now(), limit=1
        )
        if not market:
            return PositionActionResult(
                position_id, PositionActionStatus.PENDING, attempt, "MARKET_PRICE_UNAVAILABLE"
            )
        # A confirmed fill is booked once: that booking is the attempt turning CONFIRMED.
        # A later cycle re-reading the same confirmed attempt is no new success.
        first_booking = self._accounting.receipt_for(command.idempotency_key) is None
        price = market[-1].price
        sales: tuple[SaleFill, ...] = ()
        exit_sale: dict[str, object] | None = None
        if command.operation is DlmmOperation.CLOSE and first_booking:
            closing = await self._closing_sales(position, command, attempt, price)
            if isinstance(closing, PositionActionResult):
                return closing  # the position closes in quote only after its sale
            sales, exit_sale = closing
        booked = await self._book.book(
            command.command_id, price, sales=sales, exit_sale=exit_sale
        )
        exited = booked.position.status is PositionStatus.CLOSED
        if exited:
            try:
                self._modes.close_position(position_id)
            except ValueError as error:
                self._log.warning(
                    "position_already_closed", position_id=position_id, **error_fields(error)
                )
        if first_booking:
            # The gateway carried this action: failures raised before it are moot.
            self._cleared.resolve(
                _CLEARED_BY_GATEWAY_SUCCESS,
                alert_subject("position", position_id),
                f"gateway {command.operation.value} of the position went through",
                raised_before=booked.applied_at,
            )
        status = PositionActionStatus.EXITED if exited else PositionActionStatus.REDUCED
        return PositionActionResult(position_id, status, attempt)

    # --- the sale after an exit (G22) ------------------------------------------------------

    async def _closing_sales(
        self,
        position: ShadowPosition,
        command: DlmmCommand,
        attempt: int,
        price: Decimal,
    ) -> PositionActionResult | tuple[tuple[SaleFill, ...], dict[str, object]]:
        """The sales to book with a confirmed CLOSE, or why the booking waits.

        The CLOSE's own sale is driven; the reductions' sales are only read (they were
        driven earlier this cycle). Each sale is marked to the price its exit was booked
        at: the CLOSE's at ``price``, a REMOVE's at its own fill price."""

        position_id = position.position_id
        record = await self._executions.get(command.command_id)
        if record is None:  # pragma: no cover - the confirmed attempt was just read
            return PositionActionResult(position_id, PositionActionStatus.PENDING, attempt)
        own = await self._drive_sale(position, _Attempt(attempt, 1, record), drive=True)
        if not own.terminal:
            return PositionActionResult(
                position_id, _SALE_ACTION_STATUS[own.status], attempt, own.detail
            )
        fills = [self._sale_fill(part, price) for part in own.parts]
        unsold = own.returned - own.sold if own.status is SaleStatus.UNSOLD else 0
        statuses = [own.status]
        for item in await self._sequence(position_id):
            if not self._booked_reduction(item):
                continue
            sale = await self._drive_sale(position, item, drive=False)
            if not sale.terminal:
                return PositionActionResult(
                    position_id, PositionActionStatus.SELLING, attempt, "REDUCTION_SALE_PENDING"
                )
            booked = self._accounting.receipt_for(item.record.command.idempotency_key)
            mark = price if booked is None else booked.fill.price
            fills.extend(self._sale_fill(part, mark) for part in sale.parts)
            unsold += sale.returned - sale.sold if sale.status is SaleStatus.UNSOLD else 0
            statuses.append(sale.status)
        summary = own.summary()
        if len(statuses) > 1:
            summary["reductions"] = [status.value for status in statuses[1:]]
        if unsold > 0 or SaleStatus.UNSOLD in statuses:
            summary["status"] = SaleStatus.UNSOLD.value
            summary["unsold_base_units"] = str(unsold)
        return tuple(fills), summary

    def _booked_reduction(self, item: "_Attempt") -> bool:
        command = item.record.command
        return (
            command.operation is DlmmOperation.REMOVE
            and item.record.phase is ExecutionPhase.CONFIRMED
            and self._accounting.receipt_for(command.idempotency_key) is not None
        )

    async def _drive_sale(
        self, position: ShadowPosition, exit_item: "_Attempt", *, drive: bool
    ) -> SaleProgress:
        """Advance the sale of what ``exit_item`` returned; ``drive=False`` only reads it.

        At most one part is sent per cycle; a part refused for its impact is halved within
        the cycle (simulations only). Nothing new starts while the latest part is live, so
        a retry, a restart or an unknown outcome can never sell twice."""

        if not self._swap.enabled:
            return SaleProgress(SaleStatus.DISABLED)
        position_id, attempt = position.position_id, exit_item.number
        returned = await self._returned(exit_item.record)
        if returned is None:
            receipt = exit_item.record.receipt
            assert receipt is not None
            if self._clock.now() - receipt.submitted_at < self._swap.settlement_timeout:
                return SaleProgress(SaleStatus.PENDING, detail="SETTLEMENT_PENDING")
            self._sale_alert(
                "EXIT_SALE_AMOUNT_UNKNOWN",
                position_id,
                attempt,
                "the chain did not tell how much base token the exit returned; it stays "
                "unsold until the operator checks the wallet",
            )
            return SaleProgress(SaleStatus.UNSOLD, detail="AMOUNT_UNKNOWN")
        if returned <= 0:
            return SaleProgress(SaleStatus.NOT_REQUIRED)
        sent = False
        for _ in range(self._swap.max_splits + 2):
            parts = await self._sale_parts(position_id, attempt)
            plan = _plan_sale(parts, returned, self._swap, self._clock.now())
            if plan.verdict is not None:
                return await self._sale_verdict(position_id, attempt, returned, plan)
            if not drive or sent:
                return SaleProgress(
                    SaleStatus.SELLING, returned, plan.sold, plan.confirmed, "NEXT_PART"
                )
            if plan.active is not None:
                progress = await self._continue_part(position_id, attempt, plan.active.record)
                if progress is not None:
                    return replace(progress, returned=returned, sold=plan.sold)
                sent = True
                continue
            step = await self._start_part(position, attempt, plan)
            if step is _BREACHED:
                continue
            if isinstance(step, SaleProgress):
                return replace(step, returned=returned, sold=plan.sold)
            sent = True
        parts = await self._sale_parts(position_id, attempt)
        plan = _plan_sale(parts, returned, self._swap, self._clock.now())
        if plan.verdict is not None:
            return await self._sale_verdict(position_id, attempt, returned, plan)
        return SaleProgress(SaleStatus.SELLING, returned, plan.sold, plan.confirmed)

    async def _returned(self, record: ExecutionRecord) -> int | None:
        """Base units the exit returned to the wallet by its chain settlement, if known."""

        receipt = record.receipt
        settlement = receipt.settlement if isinstance(receipt, GatewayReceiptV2) else None
        if settlement is None:
            try:
                settlement = await self._service.refresh_settlement(record.command.command_id)
            except Exception as error:
                self._log.warning("exit_settlement_unavailable", **error_fields(error))
                settlement = None
        return None if settlement is None else int(settlement.delta_x)

    async def _sale_verdict(
        self, position_id: str, attempt: int, returned: int, plan: _SalePlan
    ) -> SaleProgress:
        """SOLD or UNSOLD; a sold part's settlement is waited for (bounded) before booking."""

        for record in plan.confirmed:
            receipt = record.receipt
            if isinstance(receipt, GatewayReceiptV2) and receipt.settlement is None:
                try:
                    known = await self._service.refresh_settlement(record.command.command_id)
                except Exception as error:
                    self._log.warning("sale_settlement_unavailable", **error_fields(error))
                    known = None
                if known is None and (
                    self._clock.now() - receipt.submitted_at < self._swap.settlement_timeout
                ):
                    return SaleProgress(
                        SaleStatus.PENDING, returned, plan.sold, plan.confirmed,
                        "SALE_SETTLEMENT_PENDING",
                    )
        # Re-read: a settlement adopted just now is what the booking must use.
        reread: list[ExecutionRecord] = []
        for record in plan.confirmed:
            reread.append(await self._executions.get(record.command.command_id) or record)
        confirmed = tuple(reread)
        sold = sum(_sold_by(record) for record in confirmed)
        if plan.verdict is SaleStatus.UNSOLD:
            codes = {
                "PRICE_IMPACT_EXCEEDED": (
                    "EXIT_SALE_IMPACT_EXCEEDED",
                    f"{returned - sold} base units the exit returned stay unsold: even the "
                    f"smallest part quotes above the {self._swap.max_price_impact_bps} bps "
                    "impact limit or finds no liquidity in the pool; the operator decides",
                ),
                "ATTEMPTS_EXHAUSTED": (
                    "EXIT_SALE_FAILED",
                    f"{returned - sold} base units the exit returned stay unsold after "
                    f"{self._swap.attempts} failed sale attempts; the operator decides",
                ),
            }
            code, text = codes[plan.detail or "ATTEMPTS_EXHAUSTED"]
            self._sale_alert(code, position_id, attempt, text)
        return SaleProgress(plan.verdict or SaleStatus.SOLD, returned, sold, confirmed, plan.detail)

    async def _continue_part(
        self, position_id: str, attempt: int, record: ExecutionRecord
    ) -> SaleProgress | None:
        """Drive the live part; ``None`` when it was just sent (or settled) this cycle."""

        if record.phase is ExecutionPhase.AWAITING_APPROVAL:
            return SaleProgress(SaleStatus.AWAITING_APPROVAL, detail="AWAITING_APPROVAL")
        if record.phase in UNRESOLVED_PHASES:
            try:
                state = await self._service.reconcile(record.command.command_id)
            except Exception as error:
                self._log.warning("sale_reconcile_failed", **error_fields(error))
                return self._unresolved_part(
                    position_id, attempt, record, record.receipt, "SALE_OUTCOME_UNKNOWN"
                )
            outcome = settled_result(state, record.command, blocked=record.phase.value)
            if outcome.status is SubmitStatus.PENDING:
                return self._unresolved_part(
                    position_id, attempt, record, state.receipt, outcome.detail
                )
        else:
            outcome = await execute_command(self._service, record.command)
        if outcome.status is SubmitStatus.PENDING:
            return SaleProgress(SaleStatus.PENDING, detail=outcome.detail)
        if outcome.status is SubmitStatus.CONFIRMED:
            self._part_sold(position_id, attempt)
        return None

    def _unresolved_part(
        self,
        position_id: str,
        attempt: int,
        record: ExecutionRecord,
        receipt: GatewayReceipt | None,
        detail: str | None,
    ) -> SaleProgress:
        """A part still unresolved after reconciliation holds the sale (never resent).

        Past the moment its outcome should be known - its command expired while the
        executor holds no receipt for it (the executor refuses an expired command), or
        ``settlement_timeout`` since a receipt it does hold - the hold is no longer
        silent: a critical alert, once per exit attempt; the operator checks the wallet."""

        now = self._clock.now()
        overdue = (
            now >= record.command.expires_at
            if receipt is None
            else now - receipt.submitted_at >= self._swap.settlement_timeout
        )
        if overdue:
            self._sale_alert(
                "EXIT_SALE_OUTCOME_UNKNOWN",
                position_id,
                attempt,
                f"the outcome of sale part {record.command.idempotency_key} is still unknown "
                "to the executor; nothing more of this exit is sold or booked until it is "
                "known - the operator checks the wallet",
            )
        return SaleProgress(SaleStatus.PENDING, detail=detail)

    def _part_sold(self, position_id: str, attempt: int) -> None:
        """A part of this exit attempt's sale turned CONFIRMED just now: the gateway carried
        it, so the attempt's transient conditions raised before this moment are moot (spec
        7.1: no stale critical alert). Only this attempt's: from here on only later parts of
        it start, and each part's outage alerts under its own dedupe key (a later outage is
        a new open alert, never one the reporter already counts as raised)."""

        self._cleared.resolve(
            _CLEARED_BY_SALE_PART,
            _sale_subject(position_id, attempt),
            "a part of the sale after the exit went through the gateway",
            raised_before=self._clock.now(),
        )

    async def _start_part(
        self, position: ShadowPosition, attempt: int, plan: _SalePlan
    ) -> "SaleProgress | object | None":
        """Simulate the next part (or a renewed card); ``_BREACHED`` asks for a smaller one,
        ``None`` means it was sent this cycle."""

        position_id = position.position_id
        context = await self._contexts.get(position_id)
        if context is None:
            # As the exit's own start: the condition needs a person, never a silent wait.
            self._alerts.raise_alert(
                "POSITION_CONTEXT_MISSING",
                f"open position {position_id} has no durable context; the base token its "
                "exit returned cannot be sold " + alert_subject("position", position_id),
                dedupe_key=f"context:{position_id}",
            )
            return SaleProgress(SaleStatus.PENDING, detail="CONTEXT_MISSING")
        if plan.renew is not None:
            number, preview = plan.renew.number, plan.renew.preview + 1
            size = int(getattr(plan.renew.record.command, "in_amount", None) or 0)
        else:
            number, preview, size = plan.next_part, 1, plan.next_size
        command = DlmmCommandV2(
            command_id=exit_sale_command_id(position_id, attempt, number, preview),
            idempotency_key=exit_sale_key(position_id, attempt, number, preview),
            correlation_id=_correlation(context.correlation_id),
            ruleset_version=position.ruleset_version,
            mode=position.mode,
            operation=DlmmOperation.SWAP,
            pool_address=position.pool_address,
            wallet_address=context.wallet_address,
            position_address=position_id,
            expires_at=self._clock.now() + POSITION_ACTION_TTL,
            max_slippage_bps=self._swap.slippage_bps,
            swap_direction=SwapDirection.X_TO_Y,
            in_amount=str(size),
            max_price_impact_bps=self._swap.max_price_impact_bps,
        )
        self._resolver.stage(command)
        try:
            prepared = await self._service.prepare(command.command_id)
        except SimulationRejected as refused:
            if refused.code in _SPLIT_CODES:
                return _BREACHED
            return SaleProgress(SaleStatus.SELLING, detail=refused.code)
        except GatewayRejected as error:
            return self._sale_gateway_unavailable(position_id, attempt, command, error)
        except ExecutionBlocked as error:
            return await self._sale_blocked(position_id, attempt, command, error)
        except Exception as error:
            return self._sale_gateway_unavailable(position_id, attempt, command, error)
        finally:
            self._resolver.discard(command.command_id)
        if prepared.phase is ExecutionPhase.AWAITING_APPROVAL:
            return SaleProgress(SaleStatus.AWAITING_APPROVAL, detail="AWAITING_APPROVAL")
        outcome = await execute_command(self._service, command)
        if outcome.status is SubmitStatus.PENDING:
            return SaleProgress(SaleStatus.PENDING, detail=outcome.detail)
        if outcome.status is SubmitStatus.CONFIRMED:
            self._part_sold(position_id, attempt)
        return None

    async def _sale_blocked(
        self, position_id: str, attempt: int, command: DlmmCommand, error: Exception
    ) -> SaleProgress:
        """``prepare`` refused the part. A stored (FAILED) record is a part the next cycle
        counts as an attempt; without one nothing would ever count, so the sale's wait
        becomes a critical alert (once per exit attempt) instead of a silent retry."""

        try:
            record = await self._executions.get(command.command_id)
        except Exception as read_error:
            self._log.error("execution_record_unavailable", **error_fields(read_error))
            record = None
        if record is not None:
            return SaleProgress(SaleStatus.SELLING, detail=str(error))
        self._log.error("sale_blocked", position_id=position_id, **error_fields(error))
        self._sale_alert(
            "EXIT_SALE_BLOCKED",
            position_id,
            attempt,
            f"sale part {command.idempotency_key} was refused before it was recorded "
            f"({error}); the base token stays unsold until the operator checks it",
        )
        return SaleProgress(SaleStatus.SELLING, detail="SALE_BLOCKED")

    def _sale_gateway_unavailable(
        self, position_id: str, attempt: int, command: DlmmCommand, error: Exception
    ) -> SaleProgress:
        """One open alert per outage: keyed by the part (and card) it held up, so the same
        part failing every cycle raises it once, and an outage after a sold part - which a
        sold part closed for the earlier ones - raises a new one."""

        self._log.error("sale_gateway_unavailable", position_id=position_id, **error_fields(error))
        self._sale_alert(
            "EXIT_SALE_GATEWAY_UNAVAILABLE",
            position_id,
            attempt,
            "the base token the exit returned is not sold yet: the execution gateway is "
            "unavailable; the sale goes on when it answers",
            dedupe_key=f"exit_sale_gateway_unavailable:{command.idempotency_key}",
        )
        return SaleProgress(SaleStatus.PENDING, detail="GATEWAY_UNAVAILABLE")

    def _sale_alert(
        self,
        code: str,
        position_id: str,
        attempt: int,
        text: str,
        *,
        dedupe_key: str | None = None,
    ) -> None:
        self._alerts.raise_alert(
            code,
            f"position {position_id} (exit attempt {attempt}): {text} "
            + alert_subject("position", position_id)
            + " "
            + _sale_subject(position_id, attempt),
            dedupe_key=dedupe_key or f"{code.lower()}:{position_id}:{attempt}",
        )

    def _sale_fill(self, record: ExecutionRecord, mark: Decimal) -> SaleFill:
        """One confirmed part by its receipt; without a settlement (timed out) the base
        units are its exact-in amount and the proceeds its on-chain minimum output."""

        receipt = record.receipt
        assert isinstance(receipt, GatewayReceiptV2)
        simulation = record.simulation
        settlement: SettlementV2 | None = receipt.settlement
        quote = getattr(simulation, "swap_quote", None)
        if settlement is not None:
            proceeds = max(int(settlement.delta_y), 0)
        else:
            proceeds = 0 if quote is None else int(quote.min_out_amount)
        shift = 0
        if isinstance(simulation, SimulationResultV2):
            # X base units in the ledger's scale (Y's decimals), as entries are booked.
            shift = (simulation.token_y_decimals or 0) - (simulation.token_x_decimals or 0)
        network, priority = self._book.fees(receipt)
        return SaleFill(
            command_id=str(record.command.command_id),
            idempotency_key=record.command.idempotency_key,
            sold_at=receipt.submitted_at,
            sold_base_units=Decimal(_sold_by(record)).scaleb(shift),
            proceeds_base_units=Decimal(proceeds),
            mark_price=mark,
            network_fee_base_units=network,
            priority_fee_base_units=priority,
        )

    async def _sale_parts(self, position_id: str, attempt: int) -> list["_Attempt"]:
        parts: list[_Attempt] = []
        for number in range(1, sale_part_limit(self._swap) + 1):
            first = await self._executions.get_by_idempotency_key(
                exit_sale_key(position_id, attempt, number)
            )
            if first is None:
                break

            async def lookup(preview: int, part: int = number) -> ExecutionRecord | None:
                return await self._executions.get_by_idempotency_key(
                    exit_sale_key(position_id, attempt, part, preview)
                )

            found, record = await _gallop(lookup, first)
            parts.append(_Attempt(number, found, record))
        return parts


    def _operator_required(
        self, position_id: str, record: ExecutionRecord, attempt: int
    ) -> PositionActionResult:
        """A partly landed command: no automatic attempt for this position any more."""

        self._alerts.raise_alert(
            "POSITION_PARTIALLY_EXECUTED",
            f"gateway {record.command.operation.value} of position {position_id} failed "
            f"after part of its transactions was broadcast (attempt {attempt}); "
            "the position needs an operator before any further automatic action "
            + alert_subject("position", position_id),
            dedupe_key=f"partial:{position_id}",
        )
        return PositionActionResult(
            position_id, PositionActionStatus.OPERATOR_REQUIRED, attempt, "PARTIALLY_EXECUTED"
        )

    def _unavailable(
        self, position_id: str, attempt: int, error: Exception
    ) -> PositionActionResult:
        self._log.error("gateway_unavailable", position_id=position_id, **error_fields(error))
        # Once per attempt: a success closes it, and a later outage is a new alert.
        self._alerts.raise_alert(
            "EXIT_GATEWAY_UNAVAILABLE",
            f"position {position_id} stays open: the execution gateway is unavailable "
            + alert_subject("position", position_id),
            dedupe_key=f"exit-gateway-unavailable:{position_id}:{attempt}",
        )
        return PositionActionResult(
            position_id, PositionActionStatus.FAILED, attempt, "GATEWAY_UNAVAILABLE"
        )

    async def _sequence(self, position_id: str) -> list[_Attempt]:
        """Every started attempt with its latest preview (earlier previews of an attempt
        expired unapproved and sent nothing)."""

        attempts: list[_Attempt] = []
        for number in range(1, self._attempts + 1):
            first = await self._preview(position_id, number, 1)
            if first is None:
                break
            attempts.append(await self._latest_preview(position_id, number, first))
        return attempts

    async def _latest_preview(
        self, position_id: str, number: int, first: ExecutionRecord
    ) -> _Attempt:
        """Previews 1..k of an attempt are contiguous: gallop past k, then bisect."""

        async def lookup(preview: int) -> ExecutionRecord | None:
            return await self._preview(position_id, number, preview)

        found, record = await _gallop(lookup, first)
        return _Attempt(number, found, record)

    async def _preview(
        self, position_id: str, number: int, preview: int
    ) -> ExecutionRecord | None:
        return await self._executions.get_by_idempotency_key(
            position_action_key(position_id, number, preview)
        )


async def _gallop(
    lookup: Callable[[int], Awaitable[ExecutionRecord | None]], first: ExecutionRecord
) -> tuple[int, ExecutionRecord]:
    """The latest of the contiguous previews 1..k (``first`` is preview 1)."""

    found, record = 1, first
    missing = 2
    while (probe := await lookup(missing)) is not None:
        found, record = missing, probe
        missing *= 2
    while missing - found > 1:
        middle = (found + missing) // 2
        probe = await lookup(middle)
        if probe is None:
            missing = middle
        else:
            found, record = middle, probe
    return found, record


# A sale part refused for its quoted impact: the caller simulates a smaller one.
_BREACHED = object()


def _sale_subject(position_id: str, attempt: int) -> str:
    """The tag of an alert about the sale of what one exit attempt returned."""

    return alert_subject("exit-sale", f"{position_id}:{attempt}")


def _expired_unapproved(record: ExecutionRecord, now: datetime) -> bool:
    """An APPROVAL preview that expired before anybody approved it: nothing was sent."""

    return (
        record.command.mode is TradingMode.APPROVAL
        and record.approval is None
        and record.receipt is None
        and record.phase in (ExecutionPhase.AWAITING_APPROVAL, ExecutionPhase.EXPIRED)
        and record.command.expires_at <= now
    )


def _partially_executed(record: ExecutionRecord) -> bool:
    """FAILED, yet some of several transactions were broadcast: part may have landed."""

    receipt = record.receipt
    return (
        receipt is not None
        and receipt.state is GatewayState.FAILED
        and receipt.transaction_count > 1
        and bool(receipt.signatures)
    )


def _correlation(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"correlation:{value}")
