"""Pre-trade and continuous enforcement of hard portfolio invariants."""

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from src.domain.models.trading import TradingMode
from src.domain.risk.models import (
    PromotionDecision,
    PromotionEvidence,
    RiskAction,
    RiskActionKind,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskReason,
    RiskState,
    SizedPlan,
)
from src.domain.strategy.models import PositionState


class RiskEngine:
    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def pre_trade(self, plan: SizedPlan, state: RiskState) -> RiskDecision:
        reasons: list[RiskReason] = []
        if not _sized_plan_is_valid(plan, state, self._policy):
            reasons.append(RiskReason.SIZED_PLAN_INVARIANT_BREACH)
        if state.kill_switch_active:
            reasons.append(RiskReason.KILL_SWITCH_ACTIVE)
        if _daily_stop_reached(state, self._policy):
            reasons.append(RiskReason.DAILY_STOP_REACHED)
        if state.open_position_count >= self._policy.maximum_open_positions:
            reasons.append(RiskReason.POSITION_LIMIT_REACHED)
        if state.as_of >= plan.plan.expires_at:
            reasons.append(RiskReason.PLAN_EXPIRED)
        return RiskDecision(
            status=RiskDecisionStatus.REJECT if reasons else RiskDecisionStatus.APPROVE,
            reasons=tuple(reasons),
            plan=plan,
            ruleset_version=self._policy.ruleset_version,
            config_version=self._policy.config_version,
            correlation_id=plan.correlation_id,
        )

    def continuous(
        self,
        positions: Sequence[PositionState],
        state: RiskState,
    ) -> tuple[RiskAction, ...]:
        reasons: list[RiskReason] = []
        if state.kill_switch_active:
            reasons.append(RiskReason.KILL_SWITCH_ACTIVE)
        if _daily_stop_reached(state, self._policy):
            reasons.append(RiskReason.DAILY_STOP_REACHED)
        actions = [
            RiskAction(kind=RiskActionKind.BLOCK_NEW_ENTRIES, reason=reason) for reason in reasons
        ]
        for position in positions:
            actions.extend(
                RiskAction(
                    kind=RiskActionKind.ALERT,
                    reason=reason,
                    position_id=position.position_id,
                )
                for reason in reasons
            )
        return tuple(actions)


class PromotionPolicy:
    """Pure evaluation of staged-mode evidence; no mode mutation occurs here."""

    def evaluate(
        self,
        from_mode: TradingMode,
        evidence: PromotionEvidence,
    ) -> PromotionDecision:
        reasons: list[str] = []
        if from_mode is TradingMode.SHADOW:
            if evidence.full_shadow_days < 7:
                reasons.append("SHADOW_DAYS_BELOW_7")
            if evidence.completed_positions < 20:
                reasons.append("COMPLETED_POSITIONS_BELOW_20")
        if not evidence.net_expectancy_positive:
            reasons.append("NET_EXPECTANCY_NOT_POSITIVE")
        if evidence.maximum_drawdown > Decimal("0.04"):
            reasons.append("DRAWDOWN_ABOVE_4_PERCENT")
        if evidence.data_coverage < Decimal("0.99"):
            reasons.append("DATA_COVERAGE_BELOW_99_PERCENT")
        if evidence.open_critical_alerts:
            reasons.append("CRITICAL_ALERTS_OPEN")
        if not evidence.operator_confirmed:
            reasons.append("OPERATOR_CONFIRMATION_MISSING")
        return PromotionDecision(allowed=not reasons, reasons=tuple(reasons))


def _daily_stop_reached(state: RiskState, policy: RiskPolicy) -> bool:
    limit = int(
        (
            Decimal(state.start_of_day_equity_base_units) * policy.daily_stop_fraction
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    return state.conservative_daily_pnl_base_units <= -limit


def _sized_plan_is_valid(plan: SizedPlan, state: RiskState, policy: RiskPolicy) -> bool:
    """Recompute all money invariants even if an object bypassed model validation."""

    try:
        basis = plan.basis
        caps = plan.caps
        loss_fraction = plan.plan.loss_fraction_at_invalidation
        if plan.size_base_units <= 0 or not Decimal(0) < loss_fraction <= Decimal(1):
            return False
        if basis.equity_base_units > state.start_of_day_equity_base_units:
            return False
        risk_budget = _floor(
            Decimal(basis.equity_base_units) * policy.maximum_position_risk_fraction
        )
        expected_caps = (
            _floor(Decimal(basis.equity_base_units) * policy.maximum_allocation_fraction),
            _floor(Decimal(risk_budget) / loss_fraction),
            min(
                _floor(Decimal(basis.depth_base_units) * policy.maximum_liquidity_fraction),
                basis.price_impact_cap_base_units,
            ),
            max(basis.available_balance_base_units - policy.fee_reserve_base_units, 0),
        )
        actual_caps = (
            caps.allocation_base_units,
            caps.risk_base_units,
            caps.liquidity_base_units,
            caps.balance_base_units,
        )
        expected_loss = _ceil(Decimal(plan.size_base_units) * loss_fraction)
        expected_binding = tuple(
            reason
            for reason, value in (
                (RiskReason.ALLOCATION_CAP, actual_caps[0]),
                (RiskReason.POSITION_RISK_CAP, actual_caps[1]),
                (RiskReason.LIQUIDITY_CAP, actual_caps[2]),
                (RiskReason.BALANCE_CAP, actual_caps[3]),
            )
            if value == plan.size_base_units
        )
        return (
            basis.fee_reserve_base_units == policy.fee_reserve_base_units
            and basis.risk_budget_base_units == risk_budget
            and actual_caps == expected_caps
            and plan.size_base_units <= min(expected_caps)
            and plan.estimated_loss_base_units == expected_loss
            and expected_loss <= risk_budget
            and plan.binding_caps == expected_binding
            and plan.ruleset_version == policy.ruleset_version
            and plan.config_version == policy.config_version
            and plan.ruleset_version == plan.plan.ruleset_version
            and plan.config_version == plan.plan.config_version
            and plan.correlation_id == plan.plan.correlation_id
        )
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return False


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))
