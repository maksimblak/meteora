"""Position lifecycle, conservative valuation, shadow fills and performance reporting."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from src.accounting.models import (
    CostModel,
    ExitSaleModel,
    Fill,
    LedgerEntry,
    LedgerKind,
    LifecycleStep,
    SaleFill,
    ShadowReceipt,
)
from src.accounting.repository import AccountingRepository
from src.core.clock import Clock
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV1,
    GatewayState,
    canonical_command_digest,
)
from src.market.models import DataQuality, Freshness, PoolSnapshot
from src.market.windows import WINDOW_MINUTES
from src.modes.models import DailyPromotionObservation
from src.portfolio.models import (
    LiquidityTranche,
    PerformanceGroup,
    PerformanceSummary,
    PositionOutcome,
    PositionStatus,
    PositionValuation,
    ShadowPosition,
    TimeWindow,
)

_BPS = Decimal(10_000)


class AccountingService:
    """Owns the lifecycle and valuation formulas used by both shadow and live fills.

    Divergence (impermanent) loss is a conservative bound valid for any DLMM bin
    distribution: each tranche is valued as if it had fully converted into the
    depreciating token at its own entry price. Real positions spread over several
    bins convert at better prices, so the modeled loss is never understated.
    """

    def mark(self, position: ShadowPosition, market: PoolSnapshot) -> PositionValuation:
        if market.pool.address.root != position.pool_address:
            raise ValueError("market snapshot does not match position pool")
        return self.value_at(position, market.price, market.observed_at)

    def value_at(
        self, position: ShadowPosition, price: Decimal, valued_at: datetime
    ) -> PositionValuation:
        if isinstance(price, float) or price <= 0:
            raise ValueError("valuation price must be a positive Decimal")
        open_hodl = Decimal(0)
        open_liquidity = Decimal(0)
        for tranche in position.tranches:
            hodl, liquidity = _tranche_values(tranche, price)
            open_hodl += hodl
            open_liquidity += liquidity
        liquidity_value = position.realized_proceeds_base_units + open_liquidity
        hodl_value = position.realized_hodl_proceeds_base_units + open_hodl
        costs = position.total_costs_base_units
        realized = (
            position.realized_proceeds_base_units
            - position.realized_cost_basis_base_units
            + position.fee_income_base_units
            - costs
        )
        unrealized = open_liquidity - position.open_cost_basis_base_units
        net = realized + unrealized
        hodl_pnl = hodl_value - position.invested_base_units
        return PositionValuation(
            position_id=position.position_id,
            valued_at=valued_at,
            market_price=price,
            invested_base_units=position.invested_base_units,
            liquidity_value_base_units=liquidity_value,
            hodl_value_base_units=hodl_value,
            fee_income_base_units=position.fee_income_base_units,
            divergence_loss_base_units=hodl_value - liquidity_value,
            slippage_base_units=position.slippage_base_units,
            network_base_units=position.network_base_units,
            jito_base_units=position.jito_base_units,
            total_costs_base_units=costs,
            realized_pnl_base_units=realized,
            unrealized_pnl_base_units=unrealized,
            net_pnl_base_units=net,
            hodl_pnl_base_units=hodl_pnl,
            excess_vs_hodl_base_units=net - hodl_pnl,
        )

    def apply_fill(
        self,
        current: ShadowPosition | None,
        command: DlmmCommandV1,
        fill: Fill,
    ) -> LifecycleStep:
        position_id = command.position_address
        if position_id is None:  # pragma: no cover - command model invariant
            raise ValueError("lifecycle commands require position_address")
        slippage_rate = Decimal(fill.slippage_bps) / _BPS
        if command.operation is DlmmOperation.OPEN:
            if current is not None:
                raise ValueError("position already exists")
            tranche = _deposit(command, fill)
            basis = tranche.cost_basis_base_units
            slippage = basis * slippage_rate
            position = ShadowPosition(
                position_id=position_id,
                pool_address=command.pool_address,
                status=PositionStatus.OPEN,
                mode=command.mode,
                ruleset_version=command.ruleset_version,
                correlation_id=str(command.correlation_id),
                opened_at=fill.filled_at,
                updated_at=fill.filled_at,
                tranches=(tranche,),
                fee_income_base_units=fill.fee_income_base_units,
                slippage_base_units=slippage,
                network_base_units=fill.network_fee_base_units,
                jito_base_units=fill.jito_tip_base_units,
            )
            return LifecycleStep(
                position=position,
                kind=LedgerKind.OPEN,
                capital_delta_base_units=basis,
                cost_basis_delta_base_units=basis,
                realized_pnl_base_units=Decimal(0),
                divergence_realized_base_units=Decimal(0),
                slippage_base_units=slippage,
            )

        if current is None or current.status is not PositionStatus.OPEN:
            raise ValueError("active position not found")
        if (
            current.pool_address != command.pool_address
            or current.ruleset_version != command.ruleset_version
            or current.mode is not command.mode
        ):
            raise ValueError("pool, ruleset and mode cannot change inside a position lifecycle")
        if fill.filled_at < current.updated_at:
            raise ValueError("fill precedes the latest position update")
        accrued = {
            "fee_income_base_units": current.fee_income_base_units + fill.fee_income_base_units,
            "network_base_units": current.network_base_units + fill.network_fee_base_units,
            "jito_base_units": current.jito_base_units + fill.jito_tip_base_units,
            "updated_at": fill.filled_at,
            "version": current.version + 1,
        }

        if command.operation is DlmmOperation.ADD:
            tranche = _deposit(command, fill)
            basis = tranche.cost_basis_base_units
            slippage = basis * slippage_rate
            position = _replace(
                current,
                tranches=(*current.tranches, tranche),
                slippage_base_units=current.slippage_base_units + slippage,
                **accrued,
            )
            return LifecycleStep(
                position=position,
                kind=LedgerKind.ADD,
                capital_delta_base_units=basis,
                cost_basis_delta_base_units=basis,
                realized_pnl_base_units=Decimal(0),
                divergence_realized_base_units=Decimal(0),
                slippage_base_units=slippage,
            )

        fraction = Decimal(command.remove_bps or 0) / _BPS
        closed = command.operation is DlmmOperation.CLOSE or fraction == 1
        removed_hodl = Decimal(0)
        removed_proceeds = Decimal(0)
        removed_basis = Decimal(0)
        remaining: list[LiquidityTranche] = []
        for tranche in current.tranches:
            hodl, liquidity = _tranche_values(tranche, fill.price)
            if closed:
                removed_hodl += hodl
                removed_proceeds += liquidity
                removed_basis += tranche.cost_basis_base_units
                continue
            removed_hodl += hodl * fraction
            removed_proceeds += liquidity * fraction
            removed_basis += tranche.cost_basis_base_units * fraction
            keep = Decimal(1) - fraction
            remaining.append(
                LiquidityTranche(
                    entry_price=tranche.entry_price,
                    quantity_x=tranche.quantity_x * keep,
                    quantity_y=tranche.quantity_y * keep,
                    cost_basis_base_units=tranche.cost_basis_base_units * keep,
                )
            )
        slippage = removed_proceeds * slippage_rate
        position = _replace(
            current,
            status=PositionStatus.CLOSED if closed else PositionStatus.OPEN,
            closed_at=fill.filled_at if closed else None,
            tranches=tuple(remaining),
            realized_proceeds_base_units=current.realized_proceeds_base_units
            + removed_proceeds,
            realized_hodl_proceeds_base_units=current.realized_hodl_proceeds_base_units
            + removed_hodl,
            realized_cost_basis_base_units=current.realized_cost_basis_base_units
            + removed_basis,
            slippage_base_units=current.slippage_base_units + slippage,
            **accrued,
        )
        return LifecycleStep(
            position=position,
            kind=LedgerKind.CLOSE if closed else LedgerKind.REMOVE,
            capital_delta_base_units=-removed_proceeds,
            cost_basis_delta_base_units=-removed_basis,
            realized_pnl_base_units=removed_proceeds - removed_basis,
            divergence_realized_base_units=removed_hodl - removed_proceeds,
            slippage_base_units=slippage,
        )

    def apply_sales(
        self,
        position: ShadowPosition,
        sales: Sequence[SaleFill],
        correlation_id: str,
    ) -> tuple[ShadowPosition, tuple[LedgerEntry, ...]]:
        """Book the sales of base tokens the position's exits returned (G22).

        The exit booked that X at its mark; a sale for less is a cost of the position
        (slippage), a sale for more adds to its realized proceeds, and every sale's
        network and priority fees are its costs. Amounts are the receipt's (in SHADOW the
        model's). The version is not advanced: sales are booked inside the exit's commit.
        """

        gains = losses = network = priority = Decimal(0)
        entries: list[LedgerEntry] = []
        for sale in sales:
            difference = sale.proceeds_base_units - sale.sold_base_units * sale.mark_price
            gain, loss = max(difference, Decimal(0)), max(-difference, Decimal(0))
            gains += gain
            losses += loss
            network += sale.network_fee_base_units
            priority += sale.priority_fee_base_units
            effective = (
                sale.proceeds_base_units / sale.sold_base_units
                if sale.sold_base_units > 0
                else sale.mark_price
            )
            entries.append(
                LedgerEntry(
                    entry_id=str(uuid5(NAMESPACE_URL, f"ledger:{sale.idempotency_key}")),
                    command_id=sale.command_id,
                    idempotency_key=sale.idempotency_key,
                    correlation_id=correlation_id,
                    position_id=position.position_id,
                    kind=LedgerKind.SWAP,
                    occurred_at=sale.sold_at,
                    price=effective,
                    capital_delta_base_units=Decimal(0),
                    cost_basis_delta_base_units=Decimal(0),
                    realized_pnl_base_units=gain,
                    divergence_realized_base_units=Decimal(0),
                    fee_income_base_units=Decimal(0),
                    slippage_base_units=loss,
                    network_base_units=sale.network_fee_base_units,
                    jito_base_units=sale.priority_fee_base_units,
                )
            )
        if not entries:
            return position, ()
        updated = _replace(
            position,
            realized_proceeds_base_units=position.realized_proceeds_base_units + gains,
            slippage_base_units=position.slippage_base_units + losses,
            network_base_units=position.network_base_units + network,
            jito_base_units=position.jito_base_units + priority,
        )
        return updated, tuple(entries)

    def close(self, position: ShadowPosition, receipt: ShadowReceipt) -> PositionOutcome:
        if position.position_id != receipt.position.position_id:
            raise ValueError("receipt does not belong to position")
        closed_at = receipt.position.closed_at
        if receipt.position.status is not PositionStatus.CLOSED or closed_at is None:
            raise ValueError("close requires a closed position receipt")
        return PositionOutcome(
            position_id=position.position_id,
            ruleset_version=position.ruleset_version,
            mode=position.mode,
            correlation_id=position.correlation_id,
            opened_at=position.opened_at,
            closed_at=closed_at,
            valuation=receipt.valuation,
        )


WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
# Model flag of a SHADOW fill (``CommandConfirmed.cost_model_flags``): the modeled priority
# fee was not booked - the pool's quote token is not wrapped SOL and no SOL price is known.
PRIORITY_FEE_UNPRICED = "PRIORITY_FEE_UNPRICED"


def priority_fee_base_units(cost_model: CostModel, market: PoolSnapshot) -> Decimal:
    """The modeled priority fee of one transaction in the pool's quote base units.

    Lamports are quote base units in a pool quoted in wrapped SOL; another quote token has
    no SOL price here, so nothing is booked for it (the cost model's tip still is) and the
    fill carries ``PRIORITY_FEE_UNPRICED`` (``cost_model_flags``)."""

    if market.pool.quote_mint.root != WRAPPED_SOL_MINT:
        return Decimal(0)
    return Decimal(cost_model.priority_fee_lamports)


def cost_model_flags(cost_model: CostModel, market: PoolSnapshot) -> tuple[str, ...]:
    """What the SHADOW cost model of a fill in ``market`` could not price."""

    if cost_model.priority_fee_lamports > 0 and market.pool.quote_mint.root != WRAPPED_SOL_MINT:
        return (PRIORITY_FEE_UNPRICED,)
    return ()


class ShadowBroker:
    """Executes SHADOW commands without an executor, wallet or network access.

    Every fill after the open also books the LP fees the position earned since its last
    fill (``shadow_fee_income``) on top of the cost model's own accrual, and every
    transaction - the fill and the modeled sale - its priority fee (``priority_fee_base_units``)
    with the tip. With the sale after an exit on (``exit_sale``, mirroring ``SWAP_AFTER_EXIT``
    the worker passes in), a REMOVE/CLOSE also books the modeled sale of the base token it
    returns, in the same commit (G22). ``preview_close`` is the same booking of a full exit
    now, without committing it (the take profit, spec 6.8).
    """

    def __init__(
        self,
        repository: AccountingRepository,
        clock: Clock,
        exit_sale: ExitSaleModel | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._accounting = AccountingService()
        self._exit_sale = exit_sale if exit_sale is not None else ExitSaleModel()

    def apply(
        self,
        command: DlmmCommandV1,
        market: PoolSnapshot,
        cost_model: CostModel,
    ) -> ShadowReceipt:
        command_digest = canonical_command_digest(command)
        existing = self._repository.receipt_for(command.idempotency_key)
        if existing is not None:
            if existing.command_digest != command_digest:
                raise ValueError("idempotency key is bound to another command")
            return existing
        now = self._clock.now()
        if command.mode is not TradingMode.SHADOW:
            raise ValueError("shadow broker accepts SHADOW commands only")
        if command.expires_at <= now:
            raise ValueError("shadow command expired")
        if command.pool_address != market.pool.address.root:
            raise ValueError("command pool does not match market snapshot")
        if (
            market.freshness is not Freshness.FRESH
            or market.data_quality in {DataQuality.INVALID, DataQuality.UNAVAILABLE}
            or market.observed_at > now
        ):
            raise ValueError("shadow fills require a fresh, valid, already observed snapshot")
        if cost_model.slippage_bps > command.max_slippage_bps:
            raise ValueError("modeled slippage exceeds the command tolerance")
        position_id = command.position_address
        if position_id is None:  # pragma: no cover - command model invariant
            raise ValueError("shadow command requires position_address")

        current = self._repository.position_for(position_id)
        fill, step, position, sale_entries = self._book(current, command, market, cost_model, now)
        valuation = self._accounting.value_at(position, fill.price, now)
        entry = _ledger_entry(command, step, fill)
        events = _lifecycle_events(
            command, step, valuation, now, flags=cost_model_flags(cost_model, market)
        )
        fill_digest = _fill_digest(command_digest, fill, market)
        receipt = ShadowReceipt(
            command=command,
            command_digest=command_digest,
            fill=fill,
            fill_digest=fill_digest,
            applied_at=now,
            phase=ExecutionPhase.CONFIRMED,
            gateway_receipt=GatewayReceiptV1(
                command_id=command.command_id,
                command_digest=command_digest,
                simulation_digest=fill_digest,
                idempotency_key=command.idempotency_key,
                state=GatewayState.CONFIRMED,
                signature=f"shadow:{fill_digest}",
                signatures=(f"shadow:{fill_digest}",),
                transaction_count=1,
                submitted_at=now,
                attempt_count=0,
            ),
            position=position,
            valuation=valuation,
            ledger_entry_ids=(entry.entry_id, *(item.entry_id for item in sale_entries)),
            event_types=tuple(event.event_type for event in events),
        )
        outcome = (
            self._accounting.close(position, receipt)
            if position.status is PositionStatus.CLOSED
            else None
        )
        return self._repository.commit(
            receipt,
            (entry, *sale_entries),
            outcome,
            events,
            expected_version=None if current is None else current.version,
        )

    def preview_close(
        self,
        position: ShadowPosition,
        market: PoolSnapshot,
        cost_model: CostModel,
        *,
        bins: tuple[int, int],
        at: datetime,
    ) -> PositionValuation:
        """The valuation after a full exit at ``market`` now, booked as ``apply`` would book a
        CLOSE - LP fees since the last fill, exit slippage, network and priority fees, the
        modeled sale of the returned base token - but never committed.

        ``bins``: the position's range in bins relative to its entry price (which bins earn
        the pool's fees). Any mode: a gateway position is estimated by the same model."""

        if market.pool.address.root != position.pool_address:
            raise ValueError("market snapshot does not match position pool")
        key = f"preview:{position.position_id}:{position.version}"
        command = DlmmCommandV1(
            command_id=uuid5(NAMESPACE_URL, f"meteora-volume-bot:{key}"),
            idempotency_key=key,
            correlation_id=uuid5(NAMESPACE_URL, f"correlation:{position.correlation_id}"),
            ruleset_version=position.ruleset_version,
            mode=position.mode,
            operation=DlmmOperation.CLOSE,
            pool_address=position.pool_address,
            wallet_address=position.pool_address,
            position_address=position.position_id,
            expires_at=at + timedelta(minutes=2),
            max_slippage_bps=cost_model.slippage_bps,
            min_bin_id=bins[0],
            max_bin_id=bins[1],
            remove_bps=10_000,
        )
        fill, _, closed, _ = self._book(position, command, market, cost_model, at)
        return self._accounting.value_at(closed, fill.price, at)

    def _book(
        self,
        current: ShadowPosition | None,
        command: DlmmCommandV1,
        market: PoolSnapshot,
        cost_model: CostModel,
        now: datetime,
    ) -> tuple[Fill, LifecycleStep, ShadowPosition, tuple[LedgerEntry, ...]]:
        """The fill of ``command`` at ``market`` and the position after it and its sale."""

        fee_income = cost_model.fee_income_base_units
        if current is not None and command.operation is not DlmmOperation.OPEN:
            fee_income += shadow_fee_income(current, command, market, now)
        fill = Fill(
            filled_at=now,
            price=market.price,
            slippage_bps=cost_model.slippage_bps,
            network_fee_base_units=cost_model.network_fee_base_units,
            jito_tip_base_units=cost_model.jito_tip_base_units
            + priority_fee_base_units(cost_model, market),
            fee_income_base_units=fee_income,
        )
        step = self._accounting.apply_fill(current, command, fill)
        position = step.position
        sale_entries: tuple[LedgerEntry, ...] = ()
        sale = self._modeled_sale(current, command, market, cost_model, fill)
        if sale is not None:
            position, sale_entries = self._accounting.apply_sales(
                position, (sale,), str(command.correlation_id)
            )
        return fill, step, position, sale_entries

    def _modeled_sale(
        self,
        current: ShadowPosition | None,
        command: DlmmCommandV1,
        market: PoolSnapshot,
        cost_model: CostModel,
        fill: Fill,
    ) -> SaleFill | None:
        """The sale of the X this exit returns by the conservative valuation, or ``None``;
        one more transaction, so one more network fee, tip and priority fee."""

        if (
            not self._exit_sale.enabled
            or current is None
            or command.operation not in (DlmmOperation.REMOVE, DlmmOperation.CLOSE)
        ):
            return None
        value = exit_base_value(current, command, fill.price)
        if value <= 0:
            return None
        fee_rate = (
            market.base_fee_rate
            if market.base_fee_rate is not None
            else self._exit_sale.unknown_pool_fee_rate
        )
        cost = value * (fee_rate + Decimal(cost_model.slippage_bps) / _BPS)
        return SaleFill(
            command_id=str(command.command_id),
            idempotency_key=f"{command.idempotency_key}:sale",
            sold_at=fill.filled_at,
            sold_base_units=value / fill.price,
            proceeds_base_units=max(value - cost, Decimal(0)),
            mark_price=fill.price,
            network_fee_base_units=cost_model.network_fee_base_units,
            priority_fee_base_units=cost_model.jito_tip_base_units
            + priority_fee_base_units(cost_model, market),
        )


class PerformanceService:
    def __init__(self, repository: AccountingRepository) -> None:
        self._repository = repository

    def summary(self, window: TimeWindow) -> PerformanceSummary:
        buckets: dict[tuple[str, TradingMode], list[PositionOutcome]] = defaultdict(list)
        for outcome in self._repository.outcomes_in(window):
            buckets[(outcome.ruleset_version, outcome.mode)].append(outcome)
        groups = [
            _group(ruleset, mode, outcomes)
            for (ruleset, mode), outcomes in sorted(
                buckets.items(), key=lambda item: (item[0][0], item[0][1].value)
            )
        ]
        return PerformanceSummary(window=window, groups=tuple(groups))

    def daily_observation(
        self,
        risk_date: date,
        *,
        ruleset_version: str,
        mode: TradingMode,
        start_equity: Decimal,
        end_equity: Decimal,
        conservative_open_equity: Decimal,
        data_coverage: Decimal,
        decision_count: int,
        risk_invariant_breaches: int,
    ) -> DailyPromotionObservation:
        """Derive one UTC day's promotion observation from the ledger, not from callers."""

        start = datetime.combine(risk_date, time.min, tzinfo=UTC)
        window = TimeWindow(start=start, end=start + timedelta(days=1))
        outcomes = [
            outcome
            for outcome in self._repository.outcomes_in(window)
            if outcome.ruleset_version == ruleset_version and outcome.mode is mode
        ]
        return DailyPromotionObservation(
            risk_date=risk_date,
            ruleset_version=ruleset_version,
            mode=mode,
            completed_positions=len(outcomes),
            net_pnl=sum(
                (outcome.valuation.net_pnl_base_units for outcome in outcomes), Decimal(0)
            ),
            start_equity=start_equity,
            end_equity=end_equity,
            conservative_open_equity=conservative_open_equity,
            data_coverage=data_coverage,
            decision_count=decision_count,
            risk_invariant_breaches=risk_invariant_breaches,
        )


def shadow_fee_income(
    position: ShadowPosition, command: DlmmCommandV1, market: PoolSnapshot, at: datetime
) -> Decimal:
    """LP fees a SHADOW position earned since its last fill, in its own base units (B7).

    The position takes the pool's LP fees (``WindowMetrics.fees``: the protocol's share is
    not paid to liquidity providers) in proportion to its share of the pool's liquidity,
    its liquidity value over the TVL: ``value * fees / tvl`` is in the position's base
    units whatever the quote token, as fees and TVL share one unit (USD). The fees of the
    holding interval come from the shortest window of ``market`` that covers it, pro rata;
    beyond the longest window only that window's fees are known and nothing is
    extrapolated. A tranche whose bins (``command``'s offsets around its entry price) do not
    hold the price at this fill earns nothing for the interval: the position is reviewed
    every cycle, and when it was in range in between is unknown.
    """

    interval = at - position.updated_at
    if interval <= timedelta(0) or market.tvl <= 0 or market.price <= 0:
        return Decimal(0)
    pool_fees = _pool_fees(market, interval)
    if pool_fees <= 0:
        return Decimal(0)
    step = Decimal(1) + Decimal(market.bin_step) / _BPS
    earning = Decimal(0)
    for tranche in position.tranches:
        lower = tranche.entry_price * step**command.min_bin_id
        upper = tranche.entry_price * step ** (command.max_bin_id + 1)
        if lower <= market.price < upper:
            earning += _tranche_values(tranche, market.price)[1]
    return earning * pool_fees / market.tvl


def exit_base_value(
    position: ShadowPosition, command: DlmmCommandV1, price: Decimal
) -> Decimal:
    """Value at ``price`` of the base token (X) a REMOVE/CLOSE of ``position`` returns by the
    conservative valuation: a tranche below its entry price is all X, one at or above it
    all Y (it sold its X on the way up)."""

    fraction = (
        Decimal(1)
        if command.operation is DlmmOperation.CLOSE
        else Decimal(command.remove_bps or 0) / _BPS
    )
    value = Decimal(0)
    for tranche in position.tranches:
        if price < tranche.entry_price:
            value += _tranche_values(tranche, price)[1] * fraction
    return value


def _pool_fees(market: PoolSnapshot, interval: timedelta) -> Decimal:
    """LP fees the pool earned over the last ``interval``: the shortest window covering it
    pro rata, or the longest window's fees when none covers it."""

    known = sorted(
        (WINDOW_MINUTES[name], metrics.fees)
        for name, metrics in market.windows.items()
        if metrics.fees is not None
    )
    if not known:
        return Decimal(0)
    held = Decimal(interval // timedelta(microseconds=1))
    for minutes, fees in known:
        length = Decimal(minutes * 60_000_000)
        if length >= held:
            return fees * held / length
    return known[-1][1]


def _tranche_values(tranche: LiquidityTranche, price: Decimal) -> tuple[Decimal, Decimal]:
    """Return (HODL value, conservative liquidity value) of one tranche at ``price``."""

    hodl = tranche.quantity_x * price + tranche.quantity_y
    if price >= tranche.entry_price:
        liquidity = tranche.quantity_x * tranche.entry_price + tranche.quantity_y
    else:
        liquidity = (tranche.quantity_x + tranche.quantity_y / tranche.entry_price) * price
    return hodl, min(liquidity, hodl)


def _deposit(command: DlmmCommandV1, fill: Fill) -> LiquidityTranche:
    quantity_x = Decimal(command.amount_x or "0")
    quantity_y = Decimal(command.amount_y or "0")
    basis = quantity_x * fill.price + quantity_y
    if basis <= 0:
        raise ValueError("liquidity deposit requires positive capital")
    return LiquidityTranche(
        entry_price=fill.price,
        quantity_x=quantity_x,
        quantity_y=quantity_y,
        cost_basis_base_units=basis,
    )


def _replace(position: ShadowPosition, **changes: Any) -> ShadowPosition:
    """Copy with full re-validation (``model_copy`` would skip invariants)."""

    return ShadowPosition(**{**dict(position), **changes})


def _ledger_entry(command: DlmmCommandV1, step: LifecycleStep, fill: Fill) -> LedgerEntry:
    return LedgerEntry(
        entry_id=str(uuid5(NAMESPACE_URL, f"ledger:{command.idempotency_key}")),
        command_id=str(command.command_id),
        idempotency_key=command.idempotency_key,
        correlation_id=str(command.correlation_id),
        position_id=step.position.position_id,
        kind=step.kind,
        occurred_at=fill.filled_at,
        price=fill.price,
        capital_delta_base_units=step.capital_delta_base_units,
        cost_basis_delta_base_units=step.cost_basis_delta_base_units,
        realized_pnl_base_units=step.realized_pnl_base_units,
        divergence_realized_base_units=step.divergence_realized_base_units,
        fee_income_base_units=fill.fee_income_base_units,
        slippage_base_units=step.slippage_base_units,
        network_base_units=fill.network_fee_base_units,
        jito_base_units=fill.jito_tip_base_units,
    )


def position_aggregate_id(position_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"position:{position_id}")


def _lifecycle_events(
    command: DlmmCommandV1,
    step: LifecycleStep,
    valuation: PositionValuation,
    now: datetime,
    *,
    flags: Sequence[str] = (),
) -> tuple[AuditEvent, ...]:
    position = step.position
    confirmed: dict[str, Any] = {
        "mode": command.mode.value,
        "operation": command.operation.value,
        "idempotency_key": command.idempotency_key,
        "position_id": position.position_id,
        "ruleset_version": command.ruleset_version,
        "ledger_kind": step.kind.value,
    }
    if flags:
        confirmed["cost_model_flags"] = list(flags)

    def event(kind: EventType, aggregate_id: UUID, payload: dict[str, Any]) -> AuditEvent:
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

    events = [event(EventType.COMMAND_CONFIRMED, command.command_id, confirmed)]
    aggregate = position_aggregate_id(position.position_id)
    if step.kind is LedgerKind.OPEN:
        events.append(
            event(
                EventType.POSITION_OPENED,
                aggregate,
                {
                    "position_id": position.position_id,
                    "pool_address": position.pool_address,
                    "mode": position.mode.value,
                    "invested_base_units": str(position.invested_base_units),
                },
            )
        )
    elif step.kind is LedgerKind.CLOSE:
        events.append(
            event(
                EventType.POSITION_CLOSED,
                aggregate,
                {
                    "position_id": position.position_id,
                    "mode": position.mode.value,
                    "net_pnl_base_units": str(valuation.net_pnl_base_units),
                    "hodl_pnl_base_units": str(valuation.hodl_pnl_base_units),
                    "fee_income_base_units": str(valuation.fee_income_base_units),
                    "divergence_loss_base_units": str(valuation.divergence_loss_base_units),
                    "total_costs_base_units": str(valuation.total_costs_base_units),
                },
            )
        )
    return tuple(events)


def _fill_digest(command_digest: str, fill: Fill, market: PoolSnapshot) -> str:
    payload = {
        "command_digest": command_digest,
        "market_observed_at": market.observed_at.astimezone(UTC).isoformat(),
        "fill": fill.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _group(ruleset: str, mode: TradingMode, outcomes: list[PositionOutcome]) -> PerformanceGroup:
    ordered = sorted(outcomes, key=lambda outcome: (outcome.closed_at, outcome.position_id))
    valuations = [outcome.valuation for outcome in ordered]
    net = sum((value.net_pnl_base_units for value in valuations), Decimal(0))
    running = Decimal(0)
    peak = Decimal(0)
    drawdown = Decimal(0)
    for value in valuations:
        running += value.net_pnl_base_units
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return PerformanceGroup(
        ruleset_version=ruleset,
        mode=mode,
        completed_positions=len(ordered),
        winning_positions=sum(1 for value in valuations if value.net_pnl_base_units > 0),
        net_pnl_base_units=net,
        hodl_pnl_base_units=sum((value.hodl_pnl_base_units for value in valuations), Decimal(0)),
        excess_vs_hodl_base_units=sum(
            (value.excess_vs_hodl_base_units for value in valuations), Decimal(0)
        ),
        fee_income_base_units=sum(
            (value.fee_income_base_units for value in valuations), Decimal(0)
        ),
        divergence_loss_base_units=sum(
            (value.divergence_loss_base_units for value in valuations), Decimal(0)
        ),
        total_costs_base_units=sum(
            (value.total_costs_base_units for value in valuations), Decimal(0)
        ),
        expectancy_base_units=net / Decimal(len(ordered)),
        maximum_drawdown_base_units=drawdown,
        first_closed_at=ordered[0].closed_at,
        last_closed_at=ordered[-1].closed_at,
    )
