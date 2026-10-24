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
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.accounting.repository import AccountingRepository
from src.accounting.service import AccountingService, ShadowBroker
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
)
from src.execution.contracts import DlmmCommandV1, DlmmOperation
from src.market.contracts import OnChainProvider
from src.market.models import AuthorityStatus, PoolSnapshot, RiskSignal, TokenRiskSnapshot
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
    ) -> None:
        """``decisions``: the plan records whose own exit rules (the volume-decay ratio of
        an overvolume Spot) the monitor applies; ``evidence`` counts invariant breaches;
        ``gateway_mode``: the mode the wired execution gateway exits positions of."""

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
        self._plan_exits: dict[str, _PlanExit] = {}
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
            for position_id in self._modes.open_positions():
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
        market = history[-1]
        valuation = AccountingService().mark(position, market)
        risk = self._daily.risk_state()
        if risk is None:  # pragma: no cover - ensure_open ran first
            return PositionReview(
                position_id, PositionActionKind.ALERT, ("RISK_DAY_NOT_OPENED",), None
            ), valuation
        state = _state(context, await self._plan_exit(context))
        signals = PositionSignals(
            current_fee_tvl=_fee_tvl(market),
            baseline_fee_tvl=context.baseline_fee_tvl,
            competing_tvl_growth=_growth(context.entry_tvl, market.tvl),
            normalized_range_position=_range_position(context, market.price),
            token_risk=_token_signal(await self._token(context, now)),
            estimated_rebalance_cost_base_units=self._cost(valuation) * 2,
            estimated_close_cost_base_units=self._cost(valuation),
            expected_remaining_fee_income_base_units=0,
            above_range_since=await self._above_since(state, context, market, now),
        )
        policy = self._modes.mode_state().active_kill_switch_policy
        action = _under_kill_switch(self._monitor.evaluate(state, market, risk, signals), policy)
        reasons = tuple(reason.value for reason in action.reasons)
        proposed = audit_event(
            EventType.POSITION_ACTION_PROPOSED,
            now,
            uuid5(NAMESPACE_URL, f"position:{position_id}"),
            correlation,
            None,
            {"position_id": position_id, "action": action.kind.value, "reasons": list(reasons)},
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
            receipt = ShadowBroker(self._accounting, self._clock).apply(
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
                )
        self._plan_exits[context.position_id] = found
        return found

    async def _above_since(
        self,
        state: PositionState,
        context: PositionContext,
        market: PoolSnapshot,
        now: datetime,
    ) -> datetime | None:
        """Start of the unbroken run of snapshots above a bid-only range, from history."""

        if state.upside_breach_grace is None or not PositionMonitor.above_range(state, market):
            return None
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
        return since

    async def _token(self, context: PositionContext, now: datetime) -> TokenRiskSnapshot | None:
        try:
            return await self._onchain.get_token_risk(PublicKey(context.base_mint))
        except ProviderError as error:
            self._log.warning("token_risk_unavailable", **error_fields(error))
            return None

    def _cost(self, valuation: PositionValuation) -> int:
        costs = self._policy.shadow_costs
        value = (
            valuation.liquidity_value_base_units * Decimal(costs.slippage_bps) / _BPS
            + costs.network_fee_base_units
            + costs.jito_tip_base_units
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
    )


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
