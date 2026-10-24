"""guide-v1 strategy choice (G15): Bid-Ask from the local high, Spot only in two cases."""

from datetime import timedelta
from decimal import Decimal

from src.domain.models.trading import TradingMode
from src.domain.risk import PortfolioState
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    GuideScreeningRules,
    PoolScreener,
)
from src.domain.strategy import (
    Direction,
    GuideStrategyRules,
    PositionActionKind,
    PositionMonitor,
    PositionSignals,
    PositionState,
    StrategyKind,
    StrategyPlan,
    StrategyPlanner,
    StrategyPolicy,
    StrategySelectionReason,
)
from src.market.models import PoolSnapshot, RiskSignal, TokenRiskSnapshot
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_features,
    guide_history,
    guide_snapshot,
    guide_token,
)
from tests.domain.test_guide_screening import screening_policy
from tests.domain.test_strategy_risk import risk_state

PORTFOLIO = PortfolioState(
    equity_base_units=100_000, available_balance_base_units=100_000, open_position_count=0
)
GUIDE_STRATEGY = StrategyPolicy(
    ruleset_version=RULESET, config_version=CONFIG, guide=GuideStrategyRules()
)
LEGACY_STRATEGY = StrategyPolicy(ruleset_version=RULESET, config_version=CONFIG)


def accepted(
    history: list[PoolSnapshot], token: TokenRiskSnapshot | None = None
) -> CandidateDecision:
    decision = PoolScreener().evaluate(
        history[-1],
        token or guide_token(),
        guide_features(history),
        screening_policy(guide=GuideScreeningRules()),
        context=DecisionContext(
            mode=TradingMode.LIVE,
            ruleset_version=RULESET,
            config_version=CONFIG,
            correlation_id="cycle-1",
        ),
        estimated_price_impact=Decimal("0.001"),
    )
    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    return decision


def plan(
    history: list[PoolSnapshot],
    token: TokenRiskSnapshot | None = None,
    policy: StrategyPolicy = GUIDE_STRATEGY,
) -> StrategyPlan:
    return StrategyPlanner().plan(accepted(history, token), history[-1], PORTFOLIO, policy)


def recorded(result: StrategyPlan) -> dict[str, str]:
    return {item.name: item.value for item in result.selection_evidence}


def lower_edge_depth(result: StrategyPlan, bin_step: int) -> Decimal:
    step = Decimal(1) + Decimal(bin_step) / Decimal(10_000)
    return Decimal(1) - step**result.lower_bin_offset


def test_default_is_bid_ask_from_the_local_high_reaching_seventy_to_eighty_percent_down() -> None:
    result = plan(guide_history(bin_step=250))

    # ceil(ln(1 / 0.25) / ln(1.025)) = 57 bins below; 1.025^-57 leaves 24.5% of the price.
    # One-sided bid (D08): the range ends at the active bin, nothing above the price.
    assert result.strategy is StrategyKind.BID_ASK
    assert result.direction is Direction.BEARISH
    assert result.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH
    assert (result.lower_bin_offset, result.upper_bin_offset) == (-57, 0)
    assert Decimal("0.70") <= lower_edge_depth(result, 250) <= Decimal("0.80")
    assert recorded(result)["range_limited_by_bins"] == "false"
    assert recorded(result)["range_side"] == "BID_ONLY"
    assert result.loss_fraction_at_invalidation == lower_edge_depth(result, 250)
    assert result.exit_policy.maximum_holding_period == GUIDE_STRATEGY.holding_horizon


def test_bid_ask_below_a_recent_high_stays_below_the_price_and_records_the_high() -> None:
    history = guide_history(prices=("1", "1", "1", "1", "1", "1.1", "0.99"), bin_step=250)

    result = plan(history)

    # ln(1.1 / 0.99) / ln(1.025) = 4.27 -> the high is 5 bins up; no liquidity goes there.
    assert result.strategy is StrategyKind.BID_ASK
    assert result.upper_bin_offset == 0
    assert result.lower_bin_offset == -57
    assert recorded(result)["local_high_price"] == "1.1"
    assert recorded(result)["bins_to_local_high"] == "5"


def test_bin_cap_bounds_the_range_and_says_so() -> None:
    result = plan(guide_history(bin_step=100))

    assert result.upper_bin_offset - result.lower_bin_offset + 1 == 70  # DLMM position width
    assert (result.lower_bin_offset, result.upper_bin_offset) == (-69, 0)
    assert recorded(result)["range_limited_by_bins"] == "true"
    assert lower_edge_depth(result, 100) < Decimal("0.70")


def test_spot_on_all_time_high_breakout_with_organic_working_volume() -> None:
    history = guide_history(
        prices=("1", "1.2", "1.1", "1", "1.1", "1.15", "1.3"),
        created_at=NOW - timedelta(minutes=33),
    )
    old_mint = guide_token(earliest_activity_at=NOW - timedelta(hours=5))

    result = plan(history, old_mint)

    assert result.strategy is StrategyKind.SPOT
    assert result.selection_reason is StrategySelectionReason.SPOT_ATH_BREAKOUT
    # T19: one-sided like the author's - nothing above the price.
    assert result.upper_bin_offset == 0 and result.lower_bin_offset < 0
    assert result.exit_policy.maximum_holding_period == timedelta(hours=3)
    assert recorded(result)["all_time_high_price"] == "1.2"


def test_breakout_over_a_history_gap_is_not_an_ath_and_stays_bid_ask() -> None:
    """Review probe: created 3h ago, seen at +10 min, then 2h15m without data, then 1 -> 1.3."""

    created = NOW - timedelta(hours=3)
    first = guide_snapshot(
        created + timedelta(minutes=10), short="20000", long="120000", created_at=created
    )
    recent = guide_history(
        prices=("1", "1.1", "1.2", "1.1", "1.2", "1.25", "1.3"), created_at=created
    )

    result = plan([first, *recent])

    assert result.strategy is StrategyKind.BID_ASK
    assert result.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH
    assert recorded(result)["all_time_high_price"] == "UNKNOWN"


def test_overvolume_spot_is_short_and_exits_on_the_first_volume_drop() -> None:
    result = plan(guide_history(short="600000", tvl="1000000"))

    assert result.strategy is StrategyKind.SPOT
    assert result.selection_reason is StrategySelectionReason.SPOT_OVERVOLUME
    assert result.exit_policy.maximum_holding_period == timedelta(minutes=30)
    assert result.holding_horizon == timedelta(minutes=30)
    assert result.invalidation.volume_decay_ratio == Decimal("1")


def test_spot_needs_known_organicity_otherwise_bid_ask_stays() -> None:
    history = guide_history(short="600000", tvl="1000000")
    candidate = accepted(history)
    unproven = candidate.model_copy(
        update={"features": candidate.features.model_copy(update={"fee_efficiency": None})}
    )

    result = StrategyPlanner().plan(unproven, history[-1], PORTFOLIO, GUIDE_STRATEGY)

    assert result.strategy is StrategyKind.BID_ASK
    assert recorded(result)["fee_efficiency"] == "UNKNOWN"


def test_same_candidate_is_spot_under_shadow_hypothesis_and_bid_ask_under_guide() -> None:
    """Golden pair: the T03 neutral-Spot rule survives only as shadow-hypothesis-v1."""

    history = guide_history()
    legacy = plan(history, policy=LEGACY_STRATEGY)
    guided = plan(history)

    assert legacy.strategy is StrategyKind.SPOT
    assert legacy.selection_reason is StrategySelectionReason.LEGACY_NEUTRAL_SPOT
    assert guided.strategy is StrategyKind.BID_ASK
    assert guided.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH


def test_both_bid_only_plans_carry_the_upside_exit() -> None:
    bid_ask = plan(guide_history(bin_step=250))
    # T19: the one-sided Spot is a bid-only range too.
    spot = plan(guide_history(short="600000", tvl="1000000"))

    rules = GuideStrategyRules()
    for result in (bid_ask, spot):
        assert result.exit_policy.upside_breach_grace == rules.bid_only_upside_grace
        assert result.exit_policy.upside_breach_grace == timedelta(minutes=30)
        assert result.exit_policy.upside_breach_fraction == rules.bid_only_upside_exit


def test_bid_ask_born_at_its_upper_edge_is_held_until_price_nears_the_lower_edge() -> None:
    result = plan(guide_history(bin_step=250))
    step = Decimal("1.025")
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=step**result.lower_bin_offset,
        upper_price=step**result.upper_bin_offset,  # the entry price: no bin above it
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
        upside_breach_grace=result.exit_policy.upside_breach_grace,
        upside_breach_fraction=result.exit_policy.upside_breach_fraction,
    )
    signals = {
        "current_fee_tvl": Decimal("0.02"),
        "baseline_fee_tvl": Decimal("0.02"),
        "competing_tvl_growth": Decimal("0.10"),
        "token_risk": RiskSignal.SAFE,
        "estimated_rebalance_cost_base_units": 10,
        "estimated_close_cost_base_units": 5,
        "expected_remaining_fee_income_base_units": 100,
    }
    history = guide_history(short="400")

    at_entry = PositionMonitor(price_shock_fraction=Decimal("0.80")).evaluate(
        position,
        history[-1],
        risk_state(),
        PositionSignals(normalized_range_position=Decimal("1"), **signals),  # type: ignore[arg-type]
    )
    falling = PositionMonitor(price_shock_fraction=Decimal("0.80")).evaluate(
        position,
        history[-1],
        risk_state(),
        PositionSignals(normalized_range_position=Decimal("0.05"), **signals),  # type: ignore[arg-type]
    )

    assert at_entry.kind is PositionActionKind.HOLD
    assert falling.kind is PositionActionKind.REDUCE
