"""The sale of the base token after a gateway exit (G22), through ``GatewayPositionManager``.

The executor is an in-process fake of contract V2 with the SWAP extension: idempotent by
key, receipts kept for lookups, confirmed receipts carrying the chain settlement. Its swap
quote has a linear impact (``impact_per_unit`` bps per base unit sold) and pays
``out_per_unit`` quote units per base unit, so every expected number below follows from
those inputs. Nothing reaches a network.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from prometheus_client import CollectorRegistry

from src.accounting import CostModel, InMemoryAccountingRepository, LedgerKind
from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.domain.strategy import PositionActionKind
from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommand,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV2,
    GatewayState,
    LiquidityStrategy,
    LivePromotionEvidence,
    OperatorApproval,
    SettlementV2,
    SimulationErrorV1,
    SimulationResultV2,
    SwapQuoteV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionService,
    HmacApprovalVerifier,
    sign_operator_approval,
)
from src.execution.settings import SwapSettings
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics
from src.market.universe import InMemoryUniverseStore
from src.modes import InMemoryModeRepository, KillSwitchPolicy
from src.modes.alerts import SystemAlertResolver
from src.modes.models import ModeTransition, PendingPlan, PortfolioEquitySnapshot
from src.observability.alerts import CriticalAlertReporter
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import PositionContext
from src.persistence.memory import (
    InMemoryPositionContexts,
    InMemoryRecoverableExecutionStore,
    InMemorySnapshotHistory,
)
from src.portfolio.models import PositionStatus
from src.worker.execution import (
    CommandResolver,
    EntryStatus,
    GatewayEntrySettler,
    GatewayFillBook,
    GatewayPositionManager,
    ModeKillSwitch,
    PositionActionResult,
    PositionActionStatus,
    exit_sale_key,
    shadow_address,
)

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
WSOL = "So11111111111111111111111111111111111111112"
POOL = PoolRef(
    address="LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    base_mint="SysvarC1ock11111111111111111111111111111111",
    quote_mint=WSOL,
)
WALLET = "SysvarRent111111111111111111111111111111111"
POSITION = shadow_address("t17-position")
RULESET = "guide-v1"
SECRET = b"0123456789abcdef0123456789abcdef"
FREE = CostModel(
    slippage_bps=0, network_fee_base_units=Decimal(0), jito_tip_base_units=Decimal(0)
)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class SaleExecutor:
    """Contract V2 executor with SWAP: exits return ``exit_returns`` base units each."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.simulated: list[tuple[DlmmCommand, SimulationResultV2]] = []
        self.submitted: list[DlmmCommand] = []
        self.receipts: dict[UUID, GatewayReceiptV2] = {}
        self.exit_returns: list[int] = [500]
        self.exit_amounts: dict[UUID, int] = {}
        self.impact_per_unit = Decimal("0.4")
        self.out_per_unit = 970
        self.settle_exits = True
        self.unknown_operations: set[DlmmOperation] = set()
        # Answered SUBMITTED on submit (as the real executor does), CONFIRMED on lookup.
        self.submitted_operations: set[DlmmOperation] = {DlmmOperation.OPEN}

    async def simulate(self, command: DlmmCommand) -> SimulationResultV2:
        assert isinstance(command, DlmmCommandV2)
        quote: SwapQuoteV2 | None = None
        impact = 0
        errors: tuple[SimulationErrorV1, ...] = ()
        if command.operation is DlmmOperation.SWAP:
            assert command.in_amount is not None
            sold = int(command.in_amount)
            impact = min(int(sold * self.impact_per_unit), 10_000)
            out = sold * self.out_per_unit
            quote = SwapQuoteV2(
                in_amount=command.in_amount,
                out_amount=str(out),
                min_out_amount=str(out * (10_000 - command.max_slippage_bps) // 10_000),
                fee_amount=str(sold),
            )
            if command.max_price_impact_bps is not None and impact > command.max_price_impact_bps:
                errors = (
                    SimulationErrorV1(
                        code="PRICE_IMPACT_BREACH",
                        message="quoted price impact of the swap exceeds the command limit",
                    ),
                )
        position = command.position_address or POSITION
        simulation = bind_simulation_result_v2(
            command_id=command.command_id,
            command_digest=canonical_command_digest(command),
            success=not errors,
            simulated_at=self.clock.now(),
            blockhash=None if errors else "safe-blockhash",
            last_valid_block_height=None if errors else 9,
            compute_units=0 if errors else 60_000,
            network_fee_lamports="0" if errors else "5000",
            priority_fee_lamports="0" if errors else "1200",
            price_impact_bps=impact,
            min_bin_id=command.min_bin_id,
            max_bin_id=command.max_bin_id,
            position_address=None if errors else position,
            active_bin_id=8_000,
            token_x_decimals=6,
            token_y_decimals=9,
            errors=errors,
            swap_quote=quote,
        )
        self.simulated.append((command, simulation))
        return simulation

    async def submit(
        self, command: DlmmCommand, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV2:
        del approval
        assert isinstance(command, DlmmCommandV2)
        known = self.receipts.get(command.command_id)
        if known is not None:
            return known
        self.submitted.append(command)
        simulation = next(item for cmd, item in reversed(self.simulated) if cmd == command)
        receipt = GatewayReceiptV2(
            command_id=command.command_id,
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
            idempotency_key=idempotency_key,
            state=GatewayState.CONFIRMED,
            position_address=simulation.position_address or POSITION,
            signature=f"sig-{len(self.submitted)}",
            signatures=(f"sig-{len(self.submitted)}",),
            transaction_count=1,
            submitted_at=self.clock.now(),
            attempt_count=1,
            settlement=self._settlement(command),
        )
        self.receipts[command.command_id] = receipt
        if command.operation in self.unknown_operations:
            raise TimeoutError("the executor did not answer after the request left")
        if command.operation in self.submitted_operations:
            return receipt.model_copy(update={"state": GatewayState.SUBMITTED, "settlement": None})
        return receipt

    def _settlement(self, command: DlmmCommandV2) -> SettlementV2 | None:
        if command.operation is DlmmOperation.SWAP:
            assert command.in_amount is not None
            sold = int(command.in_amount)
            return SettlementV2(
                fee_lamports="6200",
                priority_fee_lamports="1200",
                delta_x=str(-sold),
                delta_y=str(sold * self.out_per_unit),
                mint_x=POOL.base_mint.root,
                mint_y=WSOL,
            )
        if command.operation in (DlmmOperation.REMOVE, DlmmOperation.CLOSE):
            if command.command_id not in self.exit_amounts:
                self.exit_amounts[command.command_id] = self.exit_returns.pop(0)
            returned = self.exit_amounts[command.command_id]
            if not self.settle_exits:
                return None  # the chain cannot tell yet; a later lookup reads it
            return SettlementV2(
                fee_lamports="5000",
                priority_fee_lamports="0",
                delta_x=str(returned),
                delta_y="0",
                mint_x=POOL.base_mint.root,
                mint_y=WSOL,
            )
        # The OPEN: 5 000 lamports of signature fee and 2 000 of priority fee.
        return SettlementV2(
            fee_lamports="7000",
            priority_fee_lamports="2000",
            delta_x="0",
            delta_y="-1000000",
            mint_x=POOL.base_mint.root,
            mint_y=WSOL,
        )

    async def status(self, signature: str) -> ChainReceiptV1:  # pragma: no cover - unused
        raise AssertionError(signature)

    async def lookup(self, command_id: UUID) -> GatewayReceiptV2 | None:
        receipt = self.receipts.get(command_id)
        command = next((cmd for cmd in self.submitted if cmd.command_id == command_id), None)
        unsettled = receipt is not None and receipt.settlement is None
        if unsettled and isinstance(command, DlmmCommandV2):
            assert receipt is not None
            settlement = self._settlement(command)
            if settlement is not None:
                receipt = receipt.model_copy(update={"settlement": settlement})
                self.receipts[command_id] = receipt
        return receipt

    def sales(self) -> list[int]:
        return [
            int(command.in_amount or "0")
            for command in self.submitted
            if isinstance(command, DlmmCommandV2) and command.operation is DlmmOperation.SWAP
        ]

    def quoted_sales(self) -> list[int]:
        return [
            int(command.in_amount or "0")
            for command, _ in self.simulated
            if isinstance(command, DlmmCommandV2) and command.operation is DlmmOperation.SWAP
        ]

    def operations(self) -> list[DlmmOperation]:
        return [command.operation for command in self.submitted]


def snapshot(price: str, at: datetime) -> PoolSnapshot:
    return PoolSnapshot(
        pool=POOL,
        windows={
            name: WindowMetrics(volume=Decimal("1000"), fees=Decimal(0))
            for name in ("5m", "30m", "1h", "4h", "24h")
        },
        tvl=Decimal("100000"),
        depth=Decimal("50000"),
        price=Decimal(price),
        bin_step=100,
        created_at=at - timedelta(days=100),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


class World:
    def __init__(self, mode: TradingMode = TradingMode.LIVE, swap: SwapSettings | None = None):
        self.mode = mode
        self.clock = FrozenClock(NOW)
        self.modes = InMemoryModeRepository()
        self.accounting = InMemoryAccountingRepository()
        self.executions = InMemoryRecoverableExecutionStore()
        self.contexts = InMemoryPositionContexts()
        self.universe = InMemoryUniverseStore()
        self.history = InMemorySnapshotHistory(self.universe)
        self.executor = SaleExecutor(self.clock)
        self.swap = swap or SwapSettings()
        self.metrics = WorkerMetrics(CollectorRegistry())
        self.alerts = CriticalAlertReporter(
            self.modes, self.clock, self.metrics, ruleset_version=RULESET
        )
        self._promote()
        self.manager = self.new_manager()

    def new_manager(self) -> GatewayPositionManager:
        """A fresh process over the same durable stores."""

        self.resolver = resolver = CommandResolver(self.executions)
        self.service = ExecutionService(
            gateway=self.executor,
            store=self.executions,
            plans=resolver,
            clock=self.clock,
            allowed_mode=self.mode,
            approval_verifier=HmacApprovalVerifier(SECRET),
            live_promotion=promotion() if self.mode is TradingMode.LIVE else None,
            receipts=self.executor,
            kill_switch=ModeKillSwitch(self.modes),
        )
        self.book = GatewayFillBook(self.accounting, self.executions, self.clock, FREE)
        return GatewayPositionManager(
            modes=self.modes,
            accounting=self.accounting,
            contexts=self.contexts,
            history=self.history,
            executions=self.executions,
            service=self.service,
            resolver=resolver,
            book=self.book,
            alerts=self.alerts,
            cleared=SystemAlertResolver(self.modes, self.clock, RULESET),
            clock=self.clock,
            metrics=self.metrics,
            swap=self.swap,
        )

    def _promote(self) -> None:
        state = self.modes.mode_state()
        self.modes.commit_transition(
            ModeTransition(
                transition_id=uuid4(),
                from_mode=state.mode,
                to_mode=self.mode,
                occurred_at=NOW,
                operator_confirmation_id="operator",
                promotion_authorized=True,
            ),
            expected_version=state.version,
            live_promotion_id=uuid4(),
        )

    def ingest(self, price: str) -> None:
        async def stage() -> None:
            stage = await self.universe.begin(uuid4(), self.clock.now())
            await stage.stage((snapshot(price, self.clock.now()),))
            await stage.commit()

        run(stage())

    def open(self) -> str:
        """A gateway OPEN of 1 000 000 quote units, all below the price (bid), carried out
        as in production: the executor answers SUBMITTED, ``GatewayEntrySettler`` sends
        the command, reconciles it to CONFIRMED and books it from its receipt."""

        plan_id, command = self._executing_entry()
        settler = GatewayEntrySettler(
            modes=self.modes,
            executions=self.executions,
            service=self.service,
            book=self.book,
            contexts=self.contexts,
            alerts=self.alerts,
            clock=self.clock,
            metrics=self.metrics,
        )
        (sent,) = run(settler.run())
        (opened,) = run(settler.run())
        assert sent.status is EntryStatus.PENDING, sent
        assert opened.status is EntryStatus.OPENED and opened.position_id == POSITION, opened
        return POSITION

    def confirm_unbooked(self) -> str:
        """An OPEN confirmed (and its plan OPENED) outside the settler, not booked yet."""

        plan_id, command = self._executing_entry()
        run(self.service.execute(command.command_id))
        state = run(self.service.reconcile(command.command_id))
        assert state.phase is ExecutionPhase.CONFIRMED
        self.modes.confirm_entry(plan_id, POSITION)
        return POSITION

    def _executing_entry(self) -> tuple[UUID, DlmmCommandV2]:
        """An admitted, claimed entry with its context, simulated (and in APPROVAL approved)."""

        self.ingest("2")
        plan_id = uuid4()
        command = DlmmCommandV2(
            command_id=uuid4(),
            idempotency_key=f"entry:{plan_id}",
            correlation_id=uuid4(),
            ruleset_version=RULESET,
            mode=self.mode,
            operation=DlmmOperation.OPEN,
            pool_address=POOL.address.root,
            wallet_address=WALLET,
            expires_at=NOW + timedelta(minutes=2),
            max_slippage_bps=100,
            strategy=LiquidityStrategy.BID_ASK,
            min_bin_id=7_940,
            max_bin_id=8_000,
            amount_x="0",
            amount_y="1000000",
            active_bin_id=8_000,
            active_bin_tolerance=3,
            token_x_decimals=6,
            token_y_decimals=9,
        )
        self.modes.put_equity_snapshot(
            PortfolioEquitySnapshot(observed_at=NOW, equity=Decimal(10**9), ruleset_version=RULESET)
        )
        self.modes.record_risk_day(
            realized_pnl=Decimal(0),
            conservative_unrealized_pnl=Decimal(0),
            ruleset_version=RULESET,
            observed_at=NOW,
            stop_fraction=Decimal("0.04"),
        )
        plan = PendingPlan(
            plan_id=plan_id,
            correlation_id=str(command.correlation_id),
            admitted_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        assert self.modes.admit_entry(plan, 3) == ()
        self.modes.claim_entry(plan_id, NOW)
        run(
            self.contexts.put(
                PositionContext(
                    position_id=POSITION,
                    plan_id=plan_id,
                    decision_id=uuid4(),
                    pool_address=POOL.address.root,
                    base_mint=POOL.base_mint.root,
                    entry_price=Decimal(2),
                    lower_price=Decimal(1),
                    upper_price=Decimal(2),
                    entry_short_volume=Decimal(1000),
                    baseline_fee_tvl=Decimal(0),
                    entry_tvl=Decimal(100000),
                    maximum_holding_period=timedelta(hours=24),
                    opened_at=NOW,
                    mode=self.mode,
                    ruleset_version=RULESET,
                    correlation_id=str(command.correlation_id),
                    wallet_address=WALLET,
                    max_slippage_bps=100,
                    min_bin_id=7_940,
                    max_bin_id=8_000,
                )
            )
        )
        self.resolver.stage(command)
        run(self.service.prepare(command.command_id))
        self.resolver.discard(command.command_id)
        if self.mode is TradingMode.APPROVAL:
            self.approve(command.command_id)
        return plan_id, command

    def apply(self, action: PositionActionKind | None = None) -> PositionActionResult | None:
        results = run(self.manager.apply({} if action is None else {POSITION: action}))
        return results[0] if results else None

    def approve(self, command_id: UUID, service: ExecutionService | None = None) -> None:
        """The operator's one-shot approval of exactly the simulated payload."""

        record = run(self.executions.get(command_id))
        assert record is not None
        simulation = record.simulation
        approval_id = uuid4()
        approved_at = self.clock.now()
        expires_at = approved_at + timedelta(minutes=1)
        run(
            (service or self.service).approve(
                command_id,
                OperatorApproval(
                    approval_id=approval_id,
                    approved_at=approved_at,
                    expires_at=expires_at,
                    operator_id="operator",
                    command_digest=simulation.command_digest,
                    simulation_digest=simulation.simulation_digest,
                    proof=sign_operator_approval(
                        SECRET,
                        approval_id=approval_id,
                        approved_at=approved_at,
                        expires_at=expires_at,
                        operator_id="operator",
                        command_digest=simulation.command_digest,
                        simulation_digest=simulation.simulation_digest,
                    ),
                ),
            )
        )

    def approve_latest_close(self) -> None:
        command = next(
            item[0]
            for item in reversed(self.executor.simulated)
            if item[0].operation is DlmmOperation.CLOSE
        )
        self.approve(command.command_id)

    def approve_latest_sale(self) -> None:
        self.approve(self.latest_sale().command.command_id)

    def latest_sale(self) -> Any:
        command, _ = next(
            (item for item in reversed(self.executor.simulated)
             if item[0].operation is DlmmOperation.SWAP),
        )
        record = run(self.executions.get(command.command_id))
        assert record is not None
        return record

    def ledger(self) -> list[LedgerKind]:
        return [entry.kind for entry in self.accounting.ledger_entries()]

    def open_alerts(self) -> set[str]:
        return {alert.code for alert in self.modes.alerts() if alert.open}

    def closed_event(self) -> dict[str, Any]:
        (event,) = [
            event
            for event in self.accounting.outbox_events()
            if event.event_type is EventType.POSITION_CLOSED
        ]
        return dict(event.payload)


def promotion() -> LivePromotionEvidence:
    return LivePromotionEvidence(
        promotion_id=UUID(int=17),
        ruleset_version=RULESET,
        approval_cycles=10,
        receipts_reconciled=True,
        risk_invariants_preserved=True,
        dedicated_rpc_ready=True,
        wallet_ready=True,
        checklist_completed=True,
        operator_confirmation_id="operator",
        promoted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )


def exit_at_half_price(world: World) -> PositionActionResult | None:
    world.clock.advance(timedelta(minutes=1))
    world.ingest("1")
    return world.apply(PositionActionKind.EXIT)


def statuses(results: Sequence[PositionActionResult | None]) -> list[PositionActionStatus | None]:
    return [None if result is None else result.status for result in results]


# --- the three outcomes of the sale ---------------------------------------------------------


def test_the_returned_base_token_is_sold_and_the_position_closes_in_quote_by_receipts() -> None:
    world = World()
    world.open()
    opened = world.accounting.position_for(POSITION)
    # The settler booked the entry with its receipt's fees: 7000 paid, 2000 of them priority.
    assert opened is not None
    assert (opened.network_base_units, opened.jito_base_units) == (Decimal(5_000), Decimal(2_000))

    result = exit_at_half_price(world)

    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.operations() == [
        DlmmOperation.OPEN,
        DlmmOperation.CLOSE,
        DlmmOperation.SWAP,
    ]
    assert world.executor.sales() == [500]
    assert world.modes.open_positions() == ()
    position = world.accounting.position_for(POSITION)
    assert position is not None and position.status is PositionStatus.CLOSED
    assert world.ledger() == [LedgerKind.OPEN, LedgerKind.CLOSE, LedgerKind.SWAP]
    (outcome,) = world.accounting.outcomes()
    # The close returned only base token, sold for 500 * 970 quote units: the position made
    # what the sale paid less what it invested and every fee the receipts name.
    fees = 7_000 + 5_000 + 6_200
    assert outcome.valuation.net_pnl_base_units == Decimal(500 * 970 - 1_000_000 - fees)
    assert position.jito_base_units == Decimal(2_000 + 1_200)
    assert world.closed_event()["exit_sale"]["status"] == "SOLD"
    sale = world.latest_sale()
    assert sale.command.idempotency_key == exit_sale_key(POSITION, 1, 1)


def test_a_sale_above_the_impact_limit_goes_out_in_parts_within_the_limit() -> None:
    # 1000 base units quote 400 bps (> 300): halved to 500 (200 bps), then the rest.
    world = World()
    world.executor.exit_returns = [1_000]
    world.open()

    first = exit_at_half_price(world)
    second = world.apply(PositionActionKind.EXIT)

    assert statuses([first, second]) == [PositionActionStatus.SELLING, PositionActionStatus.EXITED]
    assert world.executor.quoted_sales() == [1_000, 500, 500]
    assert world.executor.sales() == [500, 500]
    assert world.ledger() == [
        LedgerKind.OPEN,
        LedgerKind.CLOSE,
        LedgerKind.SWAP,
        LedgerKind.SWAP,
    ]
    assert world.modes.open_positions() == ()


def test_when_not_even_a_part_fits_the_limit_the_token_stays_under_a_critical_alert() -> None:
    # 1000 units at 10 bps each: 1000, 500, 250, 125 all breach; the smallest allowed part
    # is ceil(1000 / 2**4) = 63, so 62 is never tried.
    world = World()
    world.executor.exit_returns = [1_000]
    world.executor.impact_per_unit = Decimal(10)
    world.open()

    result = exit_at_half_price(world)

    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.quoted_sales() == [1_000, 500, 250, 125]
    assert world.executor.sales() == []
    assert "EXIT_SALE_IMPACT_EXCEEDED" in world.open_alerts()
    assert world.executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    event = world.closed_event()["exit_sale"]
    assert (event["status"], event["unsold_base_units"]) == ("UNSOLD", "1000")
    assert world.modes.open_positions() == ()
    # Nothing more is tried: the operator decides.
    assert world.apply(PositionActionKind.EXIT) is None
    assert world.executor.quoted_sales() == [1_000, 500, 250, 125]


def test_an_entry_confirmed_outside_the_settler_is_booked_with_its_receipt_fees() -> None:
    world = World()
    world.confirm_unbooked()

    run(world.manager.book_confirmed_entries())

    position = world.accounting.position_for(POSITION)
    assert position is not None and position.status is PositionStatus.OPEN
    assert (position.network_base_units, position.jito_base_units) == (
        Decimal(5_000),
        Decimal(2_000),
    )


# --- double sale, restart and unknown outcomes ----------------------------------------------


def test_an_unknown_sale_outcome_is_reconciled_after_a_restart_and_never_sold_twice() -> None:
    world = World()
    world.executor.unknown_operations = {DlmmOperation.SWAP}
    world.open()

    pending = exit_at_half_price(world)
    world.manager = world.new_manager()  # restart
    again = world.apply(PositionActionKind.EXIT)
    world.manager = world.new_manager()  # and once more after it closed
    after = world.apply(PositionActionKind.EXIT)

    assert pending is not None and pending.status is PositionActionStatus.PENDING
    assert again is not None and again.status is PositionActionStatus.EXITED
    assert after is None
    assert world.executor.sales() == [500]
    assert world.ledger().count(LedgerKind.SWAP) == 1


def test_the_exit_is_not_held_by_a_sale_waiting_for_the_chain_settlement() -> None:
    world = World(swap=SwapSettings(settlement_timeout=timedelta(minutes=10)))
    world.executor.settle_exits = False
    world.open()

    waiting = exit_at_half_price(world)
    world.clock.advance(timedelta(minutes=11))
    unknown = world.apply(PositionActionKind.EXIT)

    assert waiting is not None and waiting.status is PositionActionStatus.PENDING
    assert world.executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert unknown is not None and unknown.status is PositionActionStatus.EXITED
    assert "EXIT_SALE_AMOUNT_UNKNOWN" in world.open_alerts()
    assert world.closed_event()["exit_sale"]["status"] == "UNSOLD"
    assert world.executor.sales() == []


# --- modes, kill switch and the setting -----------------------------------------------------


def test_approval_mode_sends_each_part_only_after_its_own_approval() -> None:
    world = World(mode=TradingMode.APPROVAL)
    world.open()

    waiting = exit_at_half_price(world)  # the CLOSE awaits its own approval
    world.approve_latest_close()
    sale_card = world.apply(PositionActionKind.EXIT)
    unapproved = world.apply(PositionActionKind.EXIT)
    world.approve_latest_sale()
    sold = world.apply(PositionActionKind.EXIT)

    assert waiting is not None and waiting.status is PositionActionStatus.AWAITING_APPROVAL
    assert sale_card is not None and sale_card.status is PositionActionStatus.AWAITING_APPROVAL
    assert unapproved is not None and unapproved.status is PositionActionStatus.AWAITING_APPROVAL
    assert sold is not None and sold.status is PositionActionStatus.EXITED
    assert world.executor.sales() == [500]


def test_an_expired_unapproved_sale_card_is_renewed_without_spending_an_attempt() -> None:
    world = World(mode=TradingMode.APPROVAL)
    world.open()
    exit_at_half_price(world)
    world.approve_latest_close()
    world.apply(PositionActionKind.EXIT)  # the first card of part 1

    world.clock.advance(timedelta(minutes=3))
    renewed = world.apply(PositionActionKind.EXIT)

    assert renewed is not None and renewed.status is PositionActionStatus.AWAITING_APPROVAL
    assert world.latest_sale().command.idempotency_key == exit_sale_key(POSITION, 1, 1, 2)
    assert world.executor.sales() == []


def test_the_kill_switch_does_not_hold_the_start_of_a_sale_after_an_exit() -> None:
    # The CLOSE is confirmed before the chain tells what it returned; the operator then
    # stops everything with HOLD_AND_ALERT. The sale still starts and goes out.
    world = World()
    world.executor.settle_exits = False
    world.open()
    waiting = exit_at_half_price(world)
    world.modes.activate_kill_switch(
        reason="operator",
        policy=KillSwitchPolicy.HOLD_AND_ALERT,
        occurred_at=world.clock.now(),
        ruleset_version=RULESET,
    )
    world.executor.settle_exits = True

    result = world.apply()

    assert waiting is not None and waiting.status is PositionActionStatus.PENDING
    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.sales() == [500]


def test_with_the_sale_switched_off_the_exit_books_at_once_and_marks_the_token_unsold() -> None:
    world = World(swap=SwapSettings(enabled=False))
    world.open()

    result = exit_at_half_price(world)

    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert world.ledger() == [LedgerKind.OPEN, LedgerKind.CLOSE]
    assert world.closed_event()["exit_sale"]["status"] == "DISABLED"


def test_a_reduction_sells_its_tokens_and_the_close_books_both_sales() -> None:
    world = World()
    world.executor.exit_returns = [200, 300]
    world.open()
    world.clock.advance(timedelta(minutes=1))
    world.ingest("1")

    reduced = world.apply(PositionActionKind.REDUCE)
    world.clock.advance(timedelta(minutes=1))
    world.ingest("1")
    exited = world.apply(PositionActionKind.EXIT)

    assert reduced is not None and reduced.status is PositionActionStatus.REDUCED
    assert exited is not None and exited.status is PositionActionStatus.EXITED
    assert world.executor.sales() == [200, 300]
    assert world.ledger() == [
        LedgerKind.OPEN,
        LedgerKind.REMOVE,
        LedgerKind.CLOSE,
        LedgerKind.SWAP,
        LedgerKind.SWAP,
    ]
    assert world.executor.operations() == [
        DlmmOperation.OPEN,
        DlmmOperation.REMOVE,
        DlmmOperation.SWAP,
        DlmmOperation.CLOSE,
        DlmmOperation.SWAP,
    ]
