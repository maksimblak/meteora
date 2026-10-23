"""Deterministic strategy selection, bin range, and exit decisions."""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

from src.domain.screening import CandidateDecision, CandidateStatus, EvidenceItem
from src.domain.strategy.models import (
    Direction,
    ExitPolicy,
    GuideStrategyRules,
    InvalidationPolicy,
    PositionAction,
    PositionActionKind,
    PositionReason,
    PositionSignals,
    PositionState,
    StrategyKind,
    StrategyPlan,
    StrategyPolicy,
    StrategySelectionReason,
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
        if policy.guide is not None:
            return _guide_plan(candidate, market, policy, policy.guide)

        # shadow-hypothesis-v1: neutral -> Spot, confirmed direction -> Bid-Ask (spec 6.4).
        direction = _direction(candidate, policy)
        strategy = StrategyKind.SPOT if direction is Direction.NEUTRAL else StrategyKind.BID_ASK
        span = _bin_span(candidate, market, policy, policy.holding_horizon, policy.maximum_bins)
        if direction is Direction.BULLISH:
            lower, upper = -1, span
        elif direction is Direction.BEARISH:
            lower, upper = -span, 1
        else:
            lower, upper = -span, span
        return _assemble(
            candidate,
            market,
            policy,
            strategy=strategy,
            direction=direction,
            bounds=(lower, upper),
            horizon=policy.holding_horizon,
            loss_fraction=_volatility_loss(candidate, policy),
            volume_decay_ratio=policy.volume_decay_ratio,
            reason=(
                StrategySelectionReason.LEGACY_NEUTRAL_SPOT
                if strategy is StrategyKind.SPOT
                else StrategySelectionReason.LEGACY_DIRECTIONAL_BID_ASK
            ),
            evidence=(),
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

    @staticmethod
    def above_range(position: PositionState, market: PoolSnapshot) -> bool:
        """The price is above the range: for a bid-only range, above its top bin's upper
        border (the entry bin spans one bin step above the entry price)."""

        return market.price > _upper_border(position, market)

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
        if market.price < position.lower_price or _above_breached(
            position, market, risk, signals
        ):
            exit_reasons.append(PositionReason.RANGE_BREACH)
        if risk.as_of - position.opened_at >= position.maximum_holding_period:
            exit_reasons.append(PositionReason.MAX_HOLDING_PERIOD)
        # The short window is reported or derived from the counters (D07); the decay ratio
        # is the position plan's own when it carries one (an overvolume Spot: 1).
        short = market.windows.get("5m")
        ratio = (
            position.volume_decay_ratio
            if position.volume_decay_ratio is not None
            else self._volume_decay_ratio
        )
        if short is not None and short.volume < position.entry_short_volume * ratio:
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
        # An edge counts only if the price has moved towards it: a one-sided Bid-Ask is
        # opened next to its upper edge by design and must not be reduced at entry.
        entry_position = (position.entry_price - position.lower_price) / (
            position.upper_price - position.lower_price
        )
        lower_limit = self._near_edge_fraction
        upper_limit = Decimal(1) - self._near_edge_fraction
        near_edge = (signals.normalized_range_position <= lower_limit < entry_position) or (
            signals.normalized_range_position >= upper_limit > entry_position
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


def _upper_border(position: PositionState, market: PoolSnapshot) -> Decimal:
    if position.upside_breach_grace is None:
        return position.upper_price
    return position.upper_price * (Decimal(1) + Decimal(market.bin_step) / Decimal(10_000))


def _above_breached(
    position: PositionState, market: PoolSnapshot, risk: RiskState, signals: PositionSignals
) -> bool:
    """An upward range breach; a bid-only range tolerates a stay above it (D08).

    The guide rides a few pullbacks and leaves once the price has gone: above the entry
    bin's top for ``upside_breach_grace``, or ``upside_breach_fraction`` beyond it at once.
    """

    if not PositionMonitor.above_range(position, market):
        return False
    grace = position.upside_breach_grace
    fraction = position.upside_breach_fraction
    if grace is None or fraction is None:
        return True
    border = _upper_border(position, market)
    if market.price >= border * (Decimal(1) + fraction):
        return True
    since = signals.above_range_since
    return since is not None and risk.as_of - since >= grace


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
    horizon: timedelta,
    maximum: int,
) -> int:
    volatility = candidate.features.price_volatility or Decimal(0)
    bin_fraction = Decimal(market.bin_step) / Decimal(10_000)
    horizon_scale = (
        _duration_seconds(horizon) / _duration_seconds(policy.reference_horizon)
    ).sqrt()
    probability_scale = (Decimal("0.10") / policy.maximum_range_exit_probability).sqrt()
    raw = (
        volatility * policy.volatility_multiplier * horizon_scale * probability_scale / bin_fraction
    ).to_integral_value(rounding=ROUND_CEILING)
    return max(min(policy.minimum_bins, maximum), min(int(raw), maximum))


def _volatility_loss(candidate: CandidateDecision, policy: StrategyPolicy) -> Decimal:
    volatility = candidate.features.price_volatility or Decimal(0)
    return min(
        max(volatility * policy.volatility_multiplier, policy.loss_fraction_floor),
        policy.maximum_loss_fraction,
    )


def _guide_plan(
    candidate: CandidateDecision,
    market: PoolSnapshot,
    policy: StrategyPolicy,
    rules: GuideStrategyRules,
) -> StrategyPlan:
    """guide-v1 (spec 6.6): Bid-Ask from the local high unless a Spot case is proven."""

    features = candidate.features
    step = Decimal(1) + Decimal(market.bin_step) / Decimal(10_000)
    per_minute = features.volume_per_minute
    efficiency = features.fee_efficiency
    ath = features.all_time_high_price
    high = features.local_high_price
    organic = efficiency is not None and efficiency >= rules.spot_minimum_fee_efficiency
    breakout = ath is not None and market.price > ath * (Decimal(1) + rules.ath_breakout_margin)
    evidence = [
        EvidenceItem(
            name="volume_per_minute",
            value=_text(per_minute),
            threshold=(
                f"spot>={rules.spot_minimum_volume_per_minute}; "
                f"overvolume>={rules.overvolume_volume_per_minute}"
            ),
        ),
        EvidenceItem(
            name="fee_efficiency",
            value=_text(efficiency),
            threshold=str(rules.spot_minimum_fee_efficiency),
        ),
        EvidenceItem(
            name="all_time_high_price",
            value=_text(ath),
            threshold=f"breakout_margin={rules.ath_breakout_margin}",
        ),
        EvidenceItem(name="price", value=str(market.price)),
        EvidenceItem(name="local_high_price", value=_text(high)),
    ]
    spot_reason: StrategySelectionReason | None = None
    if organic and per_minute is not None and per_minute >= rules.overvolume_volume_per_minute:
        spot_reason = StrategySelectionReason.SPOT_OVERVOLUME
    elif (
        organic
        and breakout
        and per_minute is not None
        and (per_minute >= rules.spot_minimum_volume_per_minute)
    ):
        spot_reason = StrategySelectionReason.SPOT_ATH_BREAKOUT
    volatility_loss = _volatility_loss(candidate, policy)

    if spot_reason is not None:
        overvolume = spot_reason is StrategySelectionReason.SPOT_OVERVOLUME
        horizon = (
            rules.overvolume_maximum_holding_period if overvolume else rules.spot_holding_horizon
        )
        side_cap = min(policy.maximum_bins, (rules.maximum_position_bins - 1) // 2)
        span = _bin_span(candidate, market, policy, horizon, side_cap)
        depth = Decimal(1) - step ** (-span)
        evidence.append(EvidenceItem(name="range_depth", value=str(depth)))
        return _assemble(
            candidate,
            market,
            policy,
            strategy=StrategyKind.SPOT,
            direction=Direction.NEUTRAL,
            bounds=(-span, span),
            horizon=horizon,
            loss_fraction=min(max(volatility_loss, depth), Decimal(1)),
            volume_decay_ratio=(
                rules.overvolume_volume_decay_ratio if overvolume else policy.volume_decay_ratio
            ),
            reason=spot_reason,
            evidence=tuple(evidence),
        )

    # One-sided bid from the price down (D08): the range ends at the active bin, so the entry
    # is the quote token only - the executor never swaps and the wallet holds no base token.
    # Bins up to a higher local high would need base liquidity: only their distance is kept.
    target = _bins_for_depth(step, rules.bid_ask_target_depth, rules.bid_ask_maximum_depth)
    below = min(target, rules.maximum_position_bins - 1)
    to_high = 0 if high is None or high <= market.price else _bins_between(step, market.price, high)
    depth = Decimal(1) - step ** (-below)
    evidence.extend(
        (
            EvidenceItem(
                name="range_depth",
                value=str(depth),
                threshold=f"{rules.bid_ask_minimum_depth}..{rules.bid_ask_maximum_depth}",
            ),
            EvidenceItem(
                name="range_limited_by_bins",
                value=str(below < target).lower(),
                threshold=str(rules.maximum_position_bins),
            ),
            EvidenceItem(name="bins_to_local_high", value=str(to_high)),
            EvidenceItem(name="range_side", value="BID_ONLY"),
        )
    )
    return _assemble(
        candidate,
        market,
        policy,
        strategy=StrategyKind.BID_ASK,
        direction=Direction.BEARISH,
        bounds=(-below, 0),
        horizon=policy.holding_horizon,
        # Conservative: all capital converted at the entry price, marked at the lower edge.
        loss_fraction=min(max(volatility_loss, depth), Decimal(1)),
        volume_decay_ratio=policy.volume_decay_ratio,
        reason=StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH,
        evidence=tuple(evidence),
        upside_exit=(rules.bid_only_upside_grace, rules.bid_only_upside_exit),
    )


def _bins_for_depth(step: Decimal, target: Decimal, maximum: Decimal) -> int:
    """Fewest bins whose lower edge is at least ``target`` below price, not past ``maximum``."""

    raw = ((Decimal(1) / (Decimal(1) - target)).ln() / step.ln()).to_integral_value(
        rounding=ROUND_CEILING
    )
    bins = max(int(raw), 1)
    if bins > 1 and Decimal(1) - step ** (-bins) > maximum:
        bins -= 1
    return bins


def _bins_between(step: Decimal, low: Decimal, high: Decimal) -> int:
    raw = ((high / low).ln() / step.ln()).to_integral_value(rounding=ROUND_CEILING)
    return int(raw)


def _text(value: Decimal | None) -> str:
    return "UNKNOWN" if value is None else str(value)


def _assemble(
    candidate: CandidateDecision,
    market: PoolSnapshot,
    policy: StrategyPolicy,
    *,
    strategy: StrategyKind,
    direction: Direction,
    bounds: tuple[int, int],
    horizon: timedelta,
    loss_fraction: Decimal,
    volume_decay_ratio: Decimal,
    reason: StrategySelectionReason,
    evidence: tuple[EvidenceItem, ...],
    upside_exit: tuple[timedelta, Decimal] | None = None,
) -> StrategyPlan:
    expires_at = market.observed_at + policy.plan_ttl
    grace, fraction = upside_exit if upside_exit is not None else (None, None)
    return StrategyPlan(
        strategy=strategy,
        direction=direction,
        lower_bin_offset=bounds[0],
        upper_bin_offset=bounds[1],
        holding_horizon=horizon,
        range_exit_probability=policy.maximum_range_exit_probability,
        invalidation=InvalidationPolicy(
            maximum_adverse_price_move=loss_fraction,
            volume_decay_ratio=volume_decay_ratio,
            expires_at=expires_at,
        ),
        exit_policy=ExitPolicy(
            maximum_holding_period=horizon,
            upside_breach_grace=grace,
            upside_breach_fraction=fraction,
        ),
        created_at=market.observed_at,
        expires_at=expires_at,
        loss_fraction_at_invalidation=loss_fraction,
        expected_cost_rate=policy.expected_cost_rate,
        ruleset_version=policy.ruleset_version,
        config_version=policy.config_version,
        correlation_id=candidate.correlation_id,
        selection_reason=reason,
        selection_evidence=evidence,
    )


def _duration_seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86_400 + value.seconds) + Decimal(value.microseconds) / Decimal(
        1_000_000
    )
