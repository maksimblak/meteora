"""Conservative position sizing bounded by every independent capacity."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from src.domain.risk.models import (
    LiquidityState,
    PortfolioState,
    RiskPolicy,
    RiskReason,
    RiskRejection,
    SizedPlan,
    SizingBasis,
    SizingCaps,
)
from src.domain.strategy.models import StrategyPlan


class PositionSizer:
    def size(
        self,
        plan: StrategyPlan,
        portfolio: PortfolioState,
        liquidity: LiquidityState,
        policy: RiskPolicy,
    ) -> SizedPlan | RiskRejection:
        if plan.ruleset_version != policy.ruleset_version:
            raise ValueError("plan and risk ruleset versions differ")
        if plan.config_version != policy.config_version:
            raise ValueError("plan and risk config versions differ")
        if plan.blocked_by:
            # The strategy refused the plan (spec 6.8): no capacity, its codes as reasons.
            return RiskRejection(
                reasons=tuple(RiskReason(code.value) for code in plan.blocked_by),
                caps=None,
                ruleset_version=policy.ruleset_version,
                config_version=policy.config_version,
                correlation_id=plan.correlation_id,
            )
        loss_fraction = plan.loss_fraction_at_invalidation
        if not Decimal(0) < loss_fraction <= Decimal(1):
            return RiskRejection(
                reasons=(RiskReason.INVALID_LOSS_ESTIMATE,),
                caps=None,
                ruleset_version=policy.ruleset_version,
                config_version=policy.config_version,
                correlation_id=plan.correlation_id,
            )

        allocation = _floor(
            Decimal(portfolio.equity_base_units) * policy.maximum_allocation_fraction
        )
        risk_budget = _floor(
            Decimal(portfolio.equity_base_units) * policy.maximum_position_risk_fraction
        )
        risk = _floor(Decimal(risk_budget) / loss_fraction)
        depth_cap = _floor(Decimal(liquidity.depth_base_units) * policy.maximum_liquidity_fraction)
        liquidity_cap = min(depth_cap, liquidity.price_impact_cap_base_units)
        balance = max(
            portfolio.available_balance_base_units - policy.fee_reserve_base_units,
            0,
        )
        caps = SizingCaps(
            allocation_base_units=allocation,
            risk_base_units=risk,
            liquidity_base_units=liquidity_cap,
            balance_base_units=balance,
        )
        size = min(allocation, risk, liquidity_cap, balance)
        values = (
            (RiskReason.ALLOCATION_CAP, allocation),
            (RiskReason.POSITION_RISK_CAP, risk),
            (RiskReason.LIQUIDITY_CAP, liquidity_cap),
            (RiskReason.BALANCE_CAP, balance),
        )
        binding = tuple(reason for reason, value in values if value == size)
        if size <= 0:
            return RiskRejection(
                reasons=(RiskReason.ZERO_CAPACITY, *binding),
                caps=caps,
                ruleset_version=policy.ruleset_version,
                config_version=policy.config_version,
                correlation_id=plan.correlation_id,
            )
        estimated_loss = _ceil(Decimal(size) * loss_fraction)
        return SizedPlan(
            plan=plan,
            size_base_units=size,
            estimated_loss_base_units=estimated_loss,
            caps=caps,
            basis=SizingBasis(
                equity_base_units=portfolio.equity_base_units,
                available_balance_base_units=portfolio.available_balance_base_units,
                depth_base_units=liquidity.depth_base_units,
                price_impact_cap_base_units=liquidity.price_impact_cap_base_units,
                fee_reserve_base_units=policy.fee_reserve_base_units,
                risk_budget_base_units=risk_budget,
            ),
            binding_caps=binding,
            ruleset_version=policy.ruleset_version,
            config_version=policy.config_version,
            correlation_id=plan.correlation_id,
        )


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))
