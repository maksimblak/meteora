"""``PositionCycle``: mark open positions, propose and apply exits, enforce the daily stop.

Runs more often than the universe and before candidates (job priority 0). Each
cycle opens the UTC risk day if needed, reviews every open position with the T03
position monitor, applies SHADOW exits through the shadow broker (audited first),
then records realized + conservative unrealized P&L so the T04 repository can trip
the 4% daily stop atomically (blocking entries, demoting the mode, alerting).

Under an active kill switch the policy the operator chose at activation (mode state,
spec 7.3) decides: ``ORDERLY_EXIT`` turns every review into an EXIT - each position
leaves through a fresh simulation even without another reason - and ``HOLD_AND_ALERT``
keeps every position. Non-SHADOW exits of the mode the execution gateway serves are
carried out by ``GatewayPositionManager`` in the same job.

As the author follows a position (spec 6.8, T19): its pool is reread on its own every
``snapshot_interval`` (30 s) instead of waiting for the universe sweep (5 min) - the short
window summed over those rereads by the F1 rules, nothing of them stored with the
universe's history; its net P&L of leaving now (the fills' accounting: fees, slippage,
priority fees, the sale) is checked against the plan's take profit; and with a GMGN source
the developer's status and share are read every ``developer_interval`` (5 min).

The take profit applies to SHADOW positions only: the net P&L above is the SHADOW model,
and the actual value of an APPROVAL/LIVE position is not measured, so a real position is
never closed in a loss on the model's word - its review carries ``take_profit=UNMEASURED``
and the worker logs ``take_profit_unmeasured`` once per position (F4).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.accounting.models import ExitSaleModel
from src.accounting.repository import AccountingRepository
from src.accounting.service import AccountingService, ShadowBroker, priority_fee_base_units
from src.core.clock import Clock
from src.domain.models.events import EventType
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.risk.engine import RiskEngine
from src.domain.risk.models import RiskAction
from src.domain.strategy import (
    PositionAction,
    PositionActionKind,
    PositionMonitor,
    PositionReason,
    PositionSignals,
    PositionState,
    developer_sold,
)
from src.execution.contracts import DlmmCommandV1, DlmmOperation
from src.gmgn.enrichment import EnhancedSignalSource
from src.market.contracts import OnChainProvider
from src.market.models import (
    AuthorityStatus,
    EnhancedTokenSignals,
    Freshness,
    PoolRef,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)
from src.market.windows import derive_short_window
from src.modes.alerts import alert_subject
from src.modes.models import KillSwitchPolicy, RiskDayState
from src.modes.repository import ModeRepository
from src.modes.service import DailyRiskService
from src.observability.alerts import CriticalAlertReporter
from src.observability.logging import cycle_context, error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import (
    AuditLog,
    DecisionStore,
    PositionContext,
    PositionContextStore,
    SnapshotHistory,
)
from src.portfolio.models import PositionStatus, PositionValuation, ShadowPosition
from src.providers.contracts import ProviderError
from src.worker.evidence import DailyEvidence
from src.worker.execution import audit_event
from src.worker.policy import PipelinePolicy
from src.worker.portfolio import RiskDayKeeper, ShadowPortfolio

_BPS = Decimal(10_000)
DEFAULT_SNAPSHOT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEVELOPER_INTERVAL = timedelta(minutes=5)
# ``PositionActionProposed.take_profit`` of a non-SHADOW position whose plan has a take
# profit: its actual net P&L is not measured, so the take profit does not fire.
TAKE_PROFIT_UNMEASURED = "UNMEASURED"


class PoolSnapshotSource(Protocol):
    """One pool as the market data source serves it now (``current_snapshot``)."""

    async def current_snapshot(self, pool: PoolRef) -> PoolSnapshot: ...


@dataclass(frozen=True, slots=True)
class PositionReview:
    position_id: str
    action: PositionActionKind
    reasons: tuple[str, ...]
    action_id: UUID | None
    closed: bool = False


@dataclass(frozen=True, slots=True)
class PortfolioCycleReport:
    risk_day: RiskDayState | None
    reviews: tuple[PositionReview, ...]
    risk_actions: tuple[RiskAction, ...]
    realized_pnl: Decimal
    conservative_unrealized_pnl: Decimal


class PositionCycle:
    def __init__(
        self,
        *,
        modes: ModeRepository,
        accounting: AccountingRepository,
        contexts: PositionContextStore,
        history: SnapshotHistory,
        onchain: OnChainProvider,
        keeper: RiskDayKeeper,
        portfolio: ShadowPortfolio,
        audit: AuditLog,
        alerts: CriticalAlertReporter,
        policy: PipelinePolicy,
        clock: Clock,
        metrics: WorkerMetrics,
        decisions: DecisionStore | None = None,
        evidence: DailyEvidence | None = None,
        gateway_mode: TradingMode | None = None,
        snapshots: PoolSnapshotSource | None = None,
        snapshot_interval: timedelta = DEFAULT_SNAPSHOT_INTERVAL,
        developer: EnhancedSignalSource | None = None,
        developer_interval: timedelta = DEFAULT_DEVELOPER_INTERVAL,
        exit_sale: ExitSaleModel | None = None,
    ) -> None:
        """``decisions``: the plan records whose own exit rules (the volume-decay ratio of
        an overvolume Spot, the take profit) the monitor applies; ``evidence`` counts
        invariant breaches; ``gateway_mode``: the mode the wired execution gateway exits
        positions of. ``snapshots`` rereads a position's pool every ``snapshot_interval``;
        without it the monitor sees the universe's snapshots only. ``developer`` (GMGN with
        a key) is asked for the developer's status every ``developer_interval``.
        ``exit_sale``: the SHADOW sale after an exit (``SWAP_AFTER_EXIT``)."""

        if snapshot_interval <= timedelta(0) or developer_interval <= timedelta(0):
            raise ValueError("reread intervals must be positive")
        self._modes = modes
        self._accounting = accounting
        self._contexts = contexts
        self._history = history
        self._onchain = onchain
        self._keeper = keeper
        self._audit = audit
        self._alerts = alerts
        self._policy = policy
        self._clock = clock
        self._metrics = metrics
        self._portfolio = portfolio
        self._decisions = decisions
        self._evidence = evidence
        self._gateway_mode = gateway_mode
        self._snapshots = snapshots
        self._snapshot_interval = snapshot_interval
        self._developer = developer
        self._developer_interval = developer_interval
        self._exit_sale = exit_sale if exit_sale is not None else ExitSaleModel()
        # In memory only: the latest reread of each pool (whose counter marks the next one
        # continues) and when it was asked for; the developer watch of each position.
        self._fresh: dict[str, PoolSnapshot] = {}
        self._fetched_at: dict[str, datetime] = {}
        self._pools: dict[str, str] = {}
        self._above_from: dict[str, datetime] = {}
        self._developer_watch: dict[str, _DeveloperWatch] = {}
        self._plan_exits: dict[str, _PlanExit] = {}
        self._take_profit_unmeasured: set[str] = set()
        self._monitor = PositionMonitor(
            volume_decay_ratio=policy.strategy.volume_decay_ratio,
            price_shock_fraction=policy.strategy.price_shock_fraction,
        )
        self._daily = DailyRiskService(modes, clock, policy.risk.daily_stop_fraction)
        self._log = get_logger("worker.positions")

    async def run_all(self, *, correlation_id: UUID | None = None) -> PortfolioCycleReport:
        correlation = correlation_id or uuid4()
        with cycle_context(correlation_id=correlation, job="position_cycle"):
            self._keeper.ensure_open()
            now = self._clock.now()
            reviews: list[PositionReview] = []
            valuations: dict[str, PositionValuation] = {}
            open_ids = self._modes.open_positions()
            self._forget_closed(set(open_ids))
            for position_id in open_ids:
                review, valuation = await self._review(position_id, now, correlation)
                reviews.append(review)
                if valuation is not None and not review.closed:
                    valuations[position_id] = valuation
            realized, unrealized = self._daily_pnl(now, valuations)
            day = self._keeper.record(realized, unrealized)
            risk = self._daily.risk_state()
            actions: tuple[RiskAction, ...] = ()
            if risk is not None:
                actions = RiskEngine(self._policy.risk).continuous(
                    [state for state in await self._states(valuations)], risk
                )
            self._enforce_invariants(now)
            self._export(day, realized, unrealized)
            return PortfolioCycleReport(day, tuple(reviews), actions, realized, unrealized)

    def _enforce_invariants(self, now: datetime) -> None:
        """A breached hard limit is not a decision input any more: stop everything."""

        open_count = len(self._modes.open_positions())
        limit = self._policy.risk.maximum_open_positions
        if open_count <= limit or self._modes.mode_state().kill_switch_active:
            return
        self._log.critical("risk_invariant_breach", open_positions=open_count, limit=limit)
        if self._evidence is not None:
            self._evidence.breach(now)
        self._modes.activate_kill_switch(
            reason=(
                f"RISK_INVARIANT_BREACH: {open_count} open positions exceed the cap of {limit}"
            ),
            policy=self._policy.kill_switch_policy,
            occurred_at=now,
            ruleset_version=self._policy.ruleset_version,
        )

    async def run(
        self,
        position_id: str,
        observed_at: datetime | None = None,
        *,
        correlation_id: UUID | None = None,
    ) -> PositionReview:
        """``PositionCycle.run(position_id, observed_at)`` for a single position."""

        self._keeper.ensure_open()
        at = observed_at or self._clock.now()
        review, _ = await self._review(position_id, at, correlation_id or uuid4())
        return review

    async def _review(
        self, position_id: str, now: datetime, correlation: UUID
    ) -> tuple[PositionReview, PositionValuation | None]:
        context = await self._contexts.get(position_id)
        position = self._accounting.position_for(position_id)
        if context is None or position is None:
            self._alerts.raise_alert(
                "POSITION_CONTEXT_MISSING",
                f"open position {position_id} has no durable context "
                + alert_subject("position", position_id),
                dedupe_key=f"context:{position_id}",
            )
            return PositionReview(
                position_id, PositionActionKind.ALERT, ("CONTEXT_MISSING",), None
            ), None
        if position.status is PositionStatus.CLOSED:
            self._modes.close_position(position_id)
            return PositionReview(
                position_id, PositionActionKind.EXIT, ("ALREADY_CLOSED",), None, True
            ), None
        history = await self._history.history(context.pool_address, until=now, limit=1)
        if not history:
            return PositionReview(
                position_id, PositionActionKind.ALERT, ("MARKET_DATA_UNSAFE",), None
            ), None
        self._pools[position_id] = context.pool_address
        market = await self._market(history[-1], now)
        valuation = AccountingService().mark(position, market)
        risk = self._daily.risk_state()
        if risk is None:  # pragma: no cover - ensure_open ran first
            return PositionReview(
                position_id, PositionActionKind.ALERT, ("RISK_DAY_NOT_OPENED",), None
            ), valuation
        plan_exit = await self._plan_exit(context)
        state = _state(context, plan_exit)
        close_cost = self._cost(valuation, market)
        # A real position's worth is not measured here (the executor values no position):
        # its take profit never fires on the SHADOW model, and the review says so (T19).
        take_profit_unmeasured = (
            position.mode is not TradingMode.SHADOW
            and plan_exit.take_profit_fraction is not None
        )
        if take_profit_unmeasured:
            self._note_take_profit_unmeasured(position_id)
        signals = PositionSignals(
            current_fee_tvl=_fee_tvl(market),
            baseline_fee_tvl=context.baseline_fee_tvl,
            competing_tvl_growth=_growth(context.entry_tvl, market.tvl),
            normalized_range_position=_range_position(context, market.price),
            token_risk=_token_signal(await self._token(context, now)),
            estimated_rebalance_cost_base_units=close_cost * 2,
            estimated_close_cost_base_units=close_cost,
            expected_remaining_fee_income_base_units=0,
            above_range_since=await self._above_since(state, context, market, now),
            net_pnl_fraction=(
                None
                if take_profit_unmeasured
                else self._net_pnl_fraction(position, plan_exit, market, now)
            ),
            developer_sold=await self._developer_sold(context, now),
        )
        policy = self._modes.mode_state().active_kill_switch_policy
        action = _under_kill_switch(self._monitor.evaluate(state, market, risk, signals), policy)
        reasons = tuple(reason.value for reason in action.reasons)
        payload: dict[str, object] = {
            "position_id": position_id,
            "action": action.kind.value,
            "reasons": list(reasons),
        }
        if take_profit_unmeasured:
            payload["take_profit"] = TAKE_PROFIT_UNMEASURED
        proposed = audit_event(
            EventType.POSITION_ACTION_PROPOSED,
            now,
            uuid5(NAMESPACE_URL, f"position:{position_id}"),
            correlation,
            None,
            payload,
        )
        try:
            await self._audit.append(proposed)
        except Exception as error:
            self._log.critical("audit_persistence_failed", **error_fields(error))
            self._alerts.raise_alert(
                "AUDIT_PERSISTENCE_FAILED", "position action could not be audited; exit withheld"
            )
            return PositionReview(position_id, action.kind, reasons, None), valuation
        closed = False
        if action.kind is PositionActionKind.EXIT:
            closed = self._exit(position, context, market, action, policy, now)
        return PositionReview(
            position_id, action.kind, reasons, proposed.event_id, closed
        ), valuation

    def _exit(
        self,
        position: ShadowPosition,
        context: PositionContext,
        market: PoolSnapshot,
        action: PositionAction,
        policy: KillSwitchPolicy | None,
        now: datetime,
    ) -> bool:
        if policy is KillSwitchPolicy.HOLD_AND_ALERT:
            self._log.warning("exit_held_by_kill_switch_policy", position_id=position.position_id)
            return False
        if position.mode is not TradingMode.SHADOW:
            if position.mode is not self._gateway_mode:
                self._alerts.raise_alert(
                    "EXIT_REQUIRES_EXECUTION_GATEWAY",
                    f"position {position.position_id} needs an exit through the execution "
                    "gateway, which is not wired for its mode "
                    + alert_subject("position", position.position_id),
                    dedupe_key=f"exit-gateway:{position.position_id}",
                )
            # Otherwise GatewayPositionManager exits it later in this job.
            return False
        key = f"exit:{position.position_id}:{position.version}"
        command = DlmmCommandV1(
            command_id=uuid5(NAMESPACE_URL, f"meteora-volume-bot:{key}"),
            idempotency_key=key,
            correlation_id=UUID(context.correlation_id),
            ruleset_version=position.ruleset_version,
            mode=position.mode,
            operation=DlmmOperation.CLOSE,
            pool_address=position.pool_address,
            wallet_address=context.wallet_address,
            position_address=position.position_id,
            expires_at=now + timedelta(minutes=2),
            max_slippage_bps=context.max_slippage_bps,
            min_bin_id=context.min_bin_id,
            max_bin_id=context.max_bin_id,
            remove_bps=10_000,
        )
        try:
            receipt = ShadowBroker(self._accounting, self._clock, self._exit_sale).apply(
                command, market, self._policy.shadow_costs
            )
        except ValueError as error:
            self._log.warning("shadow_exit_rejected", **error_fields(error))
            self._metrics.execution(position.mode, "EXIT_FAILED")
            return False
        if receipt.position.status is PositionStatus.CLOSED:
            self._modes.close_position(position.position_id)
            self._metrics.execution(position.mode, "EXITED")
            self._log.info(
                "position_closed",
                position_id=position.position_id,
                reasons=[reason.value for reason in action.reasons],
            )
            return True
        return False

    async def _plan_exit(self, context: PositionContext) -> "_PlanExit":
        """The exit rules of the position's own plan (volume decay, bid-only upside), read
        once per position; without the record the monitor's general rules apply."""

        cached = self._plan_exits.get(context.position_id)
        if cached is not None:
            return cached
        found = _PlanExit()
        if self._decisions is not None:
            try:
                record = await self._decisions.plan(context.plan_id)
            except Exception as error:
                self._log.warning("plan_record_unavailable", **error_fields(error))
                return found  # the general rules this time; read again next cycle
            if record is not None:
                plan = record.sized_plan.plan
                found = _PlanExit(
                    volume_decay_ratio=plan.invalidation.volume_decay_ratio,
                    upside_breach_grace=plan.exit_policy.upside_breach_grace,
                    upside_breach_fraction=plan.exit_policy.upside_breach_fraction,
                    take_profit_fraction=plan.exit_policy.take_profit_fraction,
                    bins=(plan.lower_bin_offset, plan.upper_bin_offset),
                )
        self._plan_exits[context.position_id] = found
        return found

    async def _market(self, stored: PoolSnapshot, now: datetime) -> PoolSnapshot:
        """The newest view of the pool: the universe's snapshot, or the position's own
        reread of it once ``snapshot_interval`` has passed since the last one.

        A reread's short window is derived from the previous reread, whose counter marks
        hold the 30-second readings, so readings closer than the window are summed (F1) -
        a newer universe snapshot (marks at the sweep's cadence only) does not restart
        that chain. Only when the rereads' chain makes no window (the first reread, a gap
        after failures, a counter reset) is the newer universe snapshot tried instead.
        Rereads never enter the universe's history. A failed reread leaves the universe's
        snapshot (a stale one alerts as before) and waits for the next interval.
        """

        pool = stored.pool.address.root
        fresh = self._fresh.get(pool)
        latest = stored if fresh is None or fresh.source_at <= stored.source_at else fresh
        source = self._snapshots
        fetched = self._fetched_at.get(pool)
        if source is None or (fetched is not None and now - fetched < self._snapshot_interval):
            return latest
        self._fetched_at[pool] = now
        try:
            current = await source.current_snapshot(stored.pool)
        except Exception as error:
            self._log.warning("position_snapshot_unavailable", pool=pool, **error_fields(error))
            return latest
        if current.pool != stored.pool or current.source_at <= latest.source_at:
            return latest
        rules = self._policy.short_window
        derived = derive_short_window(current, fresh or stored, rules)
        if fresh is not None and latest is stored and not _has_window(derived, rules.window):
            derived = derive_short_window(current, stored, rules)
        self._fresh[pool] = derived
        return derived

    def _note_take_profit_unmeasured(self, position_id: str) -> None:
        """Once per open position: its take profit is off, the operator sees why."""

        if position_id in self._take_profit_unmeasured:
            return
        self._take_profit_unmeasured.add(position_id)
        self._log.warning(
            "take_profit_unmeasured",
            position_id=position_id,
            reason="the actual value of a non-SHADOW position is not measured; "
            "its take profit does not fire",
        )

    def _net_pnl_fraction(
        self,
        position: ShadowPosition,
        plan: "_PlanExit",
        market: PoolSnapshot,
        now: datetime,
    ) -> Decimal | None:
        """Net P&L of leaving now over the capital, as the SHADOW exit would book it (fees
        earned, slippage, network and priority fees, the sale of the returned base token);
        only for a plan with a take profit and only for a SHADOW position - a real one is
        never closed on this model (``TAKE_PROFIT_UNMEASURED``)."""

        if plan.take_profit_fraction is None or plan.bins is None:
            return None
        try:
            preview = ShadowBroker(self._accounting, self._clock, self._exit_sale).preview_close(
                position,
                market,
                self._policy.shadow_costs,
                bins=plan.bins,
                at=max(now, position.updated_at),
            )
        except ValueError as error:
            self._log.warning("take_profit_unmeasured", **error_fields(error))
            return None
        if preview.invested_base_units <= 0:
            return None
        return preview.net_pnl_base_units / preview.invested_base_units

    async def _developer_sold(self, context: PositionContext, now: datetime) -> bool:
        """GMGN says the developer sold since the entry (``developer_sold``), asked at most
        every ``developer_interval``; the entry's reading is the decision's, else the first
        fresh one here. Once sold, the position keeps the reason until it is closed."""

        source = self._developer
        if source is None:
            return False
        watch = self._developer_watch.get(context.position_id)
        if watch is not None and (watch.sold or now - watch.checked_at < self._developer_interval):
            return watch.sold
        baseline = watch.baseline if watch is not None else await self._entry_developer(context)
        try:
            reading = await source.get_token_signals(PublicKey(context.base_mint))
        except Exception as error:
            self._log.warning("developer_status_unavailable", **error_fields(error))
            self._developer_watch[context.position_id] = _DeveloperWatch(now, False, baseline)
            return False
        sold = developer_sold(baseline, reading)
        if baseline is None and reading.freshness is Freshness.FRESH:
            baseline = reading
        self._developer_watch[context.position_id] = _DeveloperWatch(now, sold, baseline)
        if sold:
            self._log.warning("developer_sold", position_id=context.position_id)
        return sold

    async def _entry_developer(self, context: PositionContext) -> EnhancedTokenSignals | None:
        """The fresh GMGN reading the entry decision was made with, if any."""

        if self._decisions is None:
            return None
        try:
            record = await self._decisions.decision(context.decision_id)
        except Exception as error:
            self._log.warning("decision_record_unavailable", **error_fields(error))
            return None
        reading = None if record is None else record.token.enhanced
        if reading is None or reading.freshness is not Freshness.FRESH:
            return None
        return reading

    def _forget_closed(self, open_ids: set[str]) -> None:
        """Drop the in-memory state of positions that are no longer open."""

        for position_id in set(self._developer_watch) - open_ids:
            del self._developer_watch[position_id]
        for position_id in set(self._plan_exits) - open_ids:
            del self._plan_exits[position_id]
        for position_id in set(self._above_from) - open_ids:
            del self._above_from[position_id]
        self._take_profit_unmeasured &= open_ids
        pools ={pool for position_id, pool in self._pools.items() if position_id in open_ids}
        self._pools = {key: value for key, value in self._pools.items() if key in open_ids}
        for pool in set(self._fresh) - pools:
            del self._fresh[pool]
        for pool in set(self._fetched_at) - pools:
            del self._fetched_at[pool]

    async def _above_since(
        self,
        state: PositionState,
        context: PositionContext,
        market: PoolSnapshot,
        now: datetime,
    ) -> datetime | None:
        """Start of the unbroken run above a bid-only range.

        Found in the stored history when the review first sees the price above, then kept
        while every review's market (rereads included) stays above; one below ends it.
        """

        position_id = context.position_id
        if state.upside_breach_grace is None or not PositionMonitor.above_range(state, market):
            self._above_from.pop(position_id, None)
            return None
        known = self._above_from.get(position_id)
        if known is not None:
            return known
        history = await self._history.history(
            context.pool_address, until=now, limit=self._policy.features.maximum_history_points
        )
        since = market.observed_at
        for snapshot in reversed(history):
            if snapshot.observed_at < context.opened_at:
                break
            if not PositionMonitor.above_range(state, snapshot):
                break
            since = min(since, snapshot.observed_at)
        self._above_from[position_id] = since
        return since

    async def _token(self, context: PositionContext, now: datetime) -> TokenRiskSnapshot | None:
        try:
            return await self._onchain.get_token_risk(PublicKey(context.base_mint))
        except ProviderError as error:
            self._log.warning("token_risk_unavailable", **error_fields(error))
            return None

    def _cost(self, valuation: PositionValuation, market: PoolSnapshot) -> int:
        costs = self._policy.shadow_costs
        value = (
            valuation.liquidity_value_base_units * Decimal(costs.slippage_bps) / _BPS
            + costs.network_fee_base_units
            + costs.jito_tip_base_units
            + priority_fee_base_units(costs, market)
        )
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    def _daily_pnl(
        self, now: datetime, valuations: dict[str, PositionValuation]
    ) -> tuple[Decimal, Decimal]:
        start = datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)
        realized = sum(
            (valuation.realized_pnl_base_units for valuation in valuations.values()), Decimal(0)
        ) + self._portfolio.realized_since(start, now + timedelta(microseconds=1))
        unrealized = sum(
            (valuation.unrealized_pnl_base_units for valuation in valuations.values()), Decimal(0)
        )
        return realized, unrealized

    async def _states(self, valuations: dict[str, PositionValuation]) -> list[PositionState]:
        states: list[PositionState] = []
        for position_id in valuations:
            context = await self._contexts.get(position_id)
            if context is not None:
                states.append(_state(context))
        return states

    def _export(self, day: RiskDayState, realized: Decimal, unrealized: Decimal) -> None:
        conservative = realized + min(unrealized, Decimal(0))
        limit = day.starting_equity * self._policy.risk.daily_stop_fraction
        loss_ratio = max(-conservative, Decimal(0)) / limit if limit > 0 else Decimal(0)
        committed = self._modes.committed_entry_count(self._clock.now())
        self._metrics.pnl(realized, min(unrealized, Decimal(0)))
        self._metrics.open_positions(len(self._modes.open_positions()))
        self._metrics.risk_utilization(
            daily_loss=loss_ratio,
            positions=Decimal(committed) / Decimal(self._policy.risk.maximum_open_positions),
        )


def _under_kill_switch(action: PositionAction, policy: KillSwitchPolicy | None) -> PositionAction:
    """``ORDERLY_EXIT``: every open position exits, whatever else the monitor saw."""

    if policy is not KillSwitchPolicy.ORDERLY_EXIT or action.kind is PositionActionKind.EXIT:
        return action
    reasons = action.reasons
    if PositionReason.KILL_SWITCH_ACTIVE not in reasons:
        reasons = (PositionReason.KILL_SWITCH_ACTIVE, *reasons)
    return PositionAction(
        kind=PositionActionKind.EXIT, reasons=reasons, position_id=action.position_id
    )


@dataclass(frozen=True, slots=True)
class _PlanExit:
    volume_decay_ratio: Decimal | None = None
    upside_breach_grace: timedelta | None = None
    upside_breach_fraction: Decimal | None = None
    take_profit_fraction: Decimal | None = None
    # The plan's range in bins relative to the entry price.
    bins: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _DeveloperWatch:
    checked_at: datetime
    sold: bool
    baseline: EnhancedTokenSignals | None


def _state(context: PositionContext, plan: _PlanExit | None = None) -> PositionState:
    rules = plan or _PlanExit()
    return PositionState(
        position_id=context.position_id,
        opened_at=context.opened_at,
        entry_price=context.entry_price,
        lower_price=context.lower_price,
        upper_price=context.upper_price,
        entry_short_volume=context.entry_short_volume,
        maximum_holding_period=context.maximum_holding_period,
        volume_decay_ratio=rules.volume_decay_ratio,
        upside_breach_grace=rules.upside_breach_grace,
        upside_breach_fraction=rules.upside_breach_fraction,
        take_profit_fraction=rules.take_profit_fraction,
    )


def _has_window(snapshot: PoolSnapshot, window: str) -> bool:
    return window in snapshot.windows


def _fee_tvl(market: PoolSnapshot) -> Decimal | None:
    window = market.windows.get("30m")
    if window is None or window.fees is None or market.tvl <= 0:
        return None
    return window.fees / market.tvl


def _growth(entry: Decimal, current: Decimal) -> Decimal:
    if entry <= 0:
        return Decimal(0)
    return max((current - entry) / entry, Decimal(0))


def _range_position(context: PositionContext, price: Decimal) -> Decimal:
    width = context.upper_price - context.lower_price
    if width <= 0:
        return Decimal(0)
    return min(max((price - context.lower_price) / width, Decimal(0)), Decimal(1))


def _token_signal(token: TokenRiskSnapshot | None) -> RiskSignal:
    if token is None:
        return RiskSignal.UNKNOWN
    if AuthorityStatus.PRESENT in (
        token.mint_authority,
        token.freeze_authority,
    ) or RiskSignal.UNSAFE in (token.blacklisted, token.verified, token.wash_trading):
        return RiskSignal.UNSAFE
    if (
        token.mint_authority is AuthorityStatus.REVOKED
        and token.freeze_authority is AuthorityStatus.REVOKED
        and token.blacklisted is RiskSignal.SAFE
        and token.verified is RiskSignal.SAFE
    ):
        return RiskSignal.SAFE
    return RiskSignal.UNKNOWN
