"""Deterministic strategy selection, bin range, and exit decisions."""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

from src.domain.screening import CandidateDecision, CandidateStatus
from src.domain.strategy.models import (
    Direction,
    ExitPolicy,
    InvalidationPolicy,
    PositionAction,
    PositionActionKind,
    PositionReason,
    PositionSignals,
    PositionState,
    StrategyKind,
    StrategyPlan,
    StrategyPolicy,
)
from src.market.models import DataQuality, Freshness, PoolSnapshot, RiskSignal

if TYPE_CHECKING:
    from src.domain.risk.models import PortfolioState, RiskState


class StrategyPlanner:
    def plan(
        self,
        candidate: CandidateDecision,
        market: PoolSnapshot,
        portfolio: PortfolioState,
        policy: StrategyPolicy,
    ) -> StrategyPlan:
        del portfolio  # sizing is deliberately owned by domain.risk
        if candidate.status is not CandidateStatus.ACCEPT:
            raise ValueError("only accepted candidates can be planned")
        if candidate.ruleset_version != policy.ruleset_version:
            raise ValueError("candidate and strategy ruleset versions differ")
        if candidate.config_version != policy.config_version:
            raise ValueError("candidate and strategy config versions differ")
        if candidate.features.observed_at != market.observed_at:
            raise ValueError("candidate and market observations differ")

        direction = _direction(candidate, policy)
        strategy = StrategyKind.SPOT if direction is Direction.NEUTRAL else StrategyKind.BID_ASK
        span = _bin_span(candidate, market, policy)
        if direction is Direction.BULLISH:
            lower, upper = -1, span
        elif direction is Direction.BEARISH:
            lower, upper = -span, 1
        else:
            lower, upper = -span, span
        volatility = candidate.features.price_volatility or Decimal(0)
        loss_fraction = min(
            max(volatility * policy.volatility_multiplier, policy.loss_fraction_floor),
            policy.maximum_loss_fraction,
        )
        expires_at = market.observed_at + policy.plan_ttl
        return StrategyPlan(
            strategy=strategy,
            direction=direction,
            lower_bin_offset=lower,
            upper_bin_offset=upper,
            holding_horizon=policy.holding_horizon,
            range_exit_probability=policy.maximum_range_exit_probability,
            invalidation=InvalidationPolicy(
                maximum_adverse_price_move=loss_fraction,
                volume_decay_ratio=policy.volume_decay_ratio,
                expires_at=expires_at,
            ),
            exit_policy=ExitPolicy(maximum_holding_period=policy.holding_horizon),
            created_at=market.observed_at,
            expires_at=expires_at,
            loss_fraction_at_invalidation=loss_fraction,
            expected_cost_rate=policy.expected_cost_rate,
            ruleset_version=policy.ruleset_version,
            config_version=policy.config_version,
            correlation_id=candidate.correlation_id,
        )


class PositionMonitor:
    def __init__(
        self,
        *,
        volume_decay_ratio: Decimal = Decimal("0.50"),
        fee_decay_ratio: Decimal = Decimal("0.50"),
        maximum_tvl_competition_growth: Decimal = Decimal("0.50"),
        near_edge_fraction: Decimal = Decimal("0.10"),
        price_shock_fraction: Decimal = Decimal("0.10"),
    ) -> None:
        if (
            isinstance(volume_decay_ratio, (float, bool))
            or not Decimal(0) < volume_decay_ratio <= 1
        ):
            raise ValueError("volume_decay_ratio must be Decimal in (0, 1]")
        if (
            isinstance(price_shock_fraction, (float, bool))
            or not Decimal(0) < price_shock_fraction <= 1
        ):
            raise ValueError("price_shock_fraction must be Decimal in (0, 1]")
        for name, value in (
            ("fee_decay_ratio", fee_decay_ratio),
            ("maximum_tvl_competition_growth", maximum_tvl_competition_growth),
            ("near_edge_fraction", near_edge_fraction),
        ):
            if isinstance(value, (float, bool)) or not Decimal(0) < value <= 1:
                raise ValueError(f"{name} must be Decimal in (0, 1]")
        self._volume_decay_ratio = volume_decay_ratio
        self._fee_decay_ratio = fee_decay_ratio
        self._maximum_tvl_competition_growth = maximum_tvl_competition_growth
        self._near_edge_fraction = near_edge_fraction
        self._price_shock_fraction = price_shock_fraction

    def evaluate(
        self,
        position: PositionState,
        market: PoolSnapshot,
        risk: RiskState,
        signals: PositionSignals,
    ) -> PositionAction:
        exit_reasons: list[PositionReason] = []
        alert_reasons: list[PositionReason] = []
        if risk.kill_switch_active:
            alert_reasons.append(PositionReason.KILL_SWITCH_ACTIVE)
        if market.freshness is Freshness.STALE or market.data_quality is not DataQuality.COMPLETE:
            alert_reasons.append(PositionReason.MARKET_DATA_UNSAFE)
        if signals.token_risk is RiskSignal.UNKNOWN:
            alert_reasons.append(PositionReason.TOKEN_RISK_UNKNOWN)
        if signals.token_risk is RiskSignal.UNSAFE:
            exit_reasons.append(PositionReason.TOKEN_RISK_DETERIORATED)
        if market.price < position.lower_price or market.price > position.upper_price:
            exit_reasons.append(PositionReason.RANGE_BREACH)
        if risk.as_of - position.opened_at >= position.maximum_holding_period:
            exit_reasons.append(PositionReason.MAX_HOLDING_PERIOD)
        short = market.windows.get("5m")
        if (
            short is not None
            and short.volume < position.entry_short_volume * self._volume_decay_ratio
        ):
            exit_reasons.append(PositionReason.VOLUME_DECAY)
        if signals.current_fee_tvl is None:
            alert_reasons.append(PositionReason.FEE_SIGNAL_UNKNOWN)
        elif signals.current_fee_tvl < signals.baseline_fee_tvl * self._fee_decay_ratio:
            exit_reasons.append(PositionReason.FEE_DECAY)
        if signals.competing_tvl_growth > self._maximum_tvl_competition_growth:
            exit_reasons.append(PositionReason.TVL_COMPETITION)
        move = abs(market.price - position.entry_price) / position.entry_price
        if move >= self._price_shock_fraction:
            exit_reasons.append(PositionReason.PRICE_SHOCK)
        if exit_reasons:
            return PositionAction(
                kind=PositionActionKind.EXIT,
                reasons=tuple(exit_reasons + alert_reasons),
                position_id=position.position_id,
            )
        near_edge = (
            signals.normalized_range_position <= self._near_edge_fraction
            or signals.normalized_range_position >= Decimal(1) - self._near_edge_fraction
        )
        if near_edge:
            rebalance_value = (
                signals.expected_remaining_fee_income_base_units
                - signals.estimated_rebalance_cost_base_units
            )
            close_value = -signals.estimated_close_cost_base_units
            if rebalance_value > close_value:
                return PositionAction(
                    kind=PositionActionKind.REDUCE,
                    reasons=(
                        PositionReason.NEAR_RANGE_EDGE,
                        PositionReason.REBALANCE_AFTER_COSTS,
                        *alert_reasons,
                    ),
                    position_id=position.position_id,
                )
            return PositionAction(
                kind=PositionActionKind.EXIT,
                reasons=(
                    PositionReason.NEAR_RANGE_EDGE,
                    PositionReason.CLOSE_BEATS_REBALANCE,
                    *alert_reasons,
                ),
                position_id=position.position_id,
            )
        if alert_reasons:
            return PositionAction(
                kind=PositionActionKind.ALERT,
                reasons=tuple(alert_reasons),
                position_id=position.position_id,
            )
        return PositionAction(
            kind=PositionActionKind.HOLD,
            reasons=(),
            position_id=position.position_id,
        )


def _direction(candidate: CandidateDecision, policy: StrategyPolicy) -> Direction:
    momentum = candidate.features.price_momentum
    confirmation = candidate.features.long_ratio
    if momentum is None or confirmation is None:
        return Direction.NEUTRAL
    if confirmation < policy.directional_confirmation_ratio:
        return Direction.NEUTRAL
    if momentum >= policy.directional_momentum_threshold:
        return Direction.BULLISH
    if momentum <= -policy.directional_momentum_threshold:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _bin_span(
    candidate: CandidateDecision,
    market: PoolSnapshot,
    policy: StrategyPolicy,
) -> int:
    volatility = candidate.features.price_volatility or Decimal(0)
    bin_fraction = Decimal(market.bin_step) / Decimal(10_000)
    horizon_scale = (
        _duration_seconds(policy.holding_horizon) / _duration_seconds(policy.reference_horizon)
    ).sqrt()
    probability_scale = (Decimal("0.10") / policy.maximum_range_exit_probability).sqrt()
    raw = (
        volatility * policy.volatility_multiplier * horizon_scale * probability_scale / bin_fraction
    ).to_integral_value(rounding=ROUND_CEILING)
    return max(policy.minimum_bins, min(int(raw), policy.maximum_bins))


def _duration_seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86_400 + value.seconds) + Decimal(value.microseconds) / Decimal(
        1_000_000
    )
