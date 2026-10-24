"""T19 (G15, spec 6.8): entries and exits as the author builds them.

Spot is one-sided like the author's: quote token only, evenly from the price down. Bid-Ask
reaches 70-80% down or the pool is refused, and only from the hour's high. Spot on an ATH
breakout reads the token's ATH (GMGN) on high caps. Exits: take profit on net P&L, holding
limits per strategy, the developer selling.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.domain.risk import LiquidityState, PositionSizer, RiskPolicy, RiskRejection
from src.domain.screening import CandidateDecision, EvidenceItem
from src.domain.strategy import (
    PositionActionKind,
    PositionMonitor,
    PositionReason,
    PositionState,
    StrategyFlag,
    StrategyKind,
    StrategyPlan,
    StrategyPlanner,
    StrategyRejection,
    StrategySelectionReason,
    developer_sold,
)
from src.market.models import (
    DeveloperStatus,
    EnhancedTokenSignals,
    Freshness,
    PoolSnapshot,
)
from src.worker.policy import guide_policy
from tests.domain.guide_fixtures import CONFIG, NOW, RULESET, guide_history, guide_token
from tests.domain.test_guide_strategy import GUIDE_STRATEGY, PORTFOLIO, accepted, recorded
from tests.domain.test_strategy_risk import position_signals, risk_state

GUIDE_V1 = guide_policy(ruleset_version=RULESET, config_version=CONFIG)


def plan_for(
    history: list[PoolSnapshot],
    *,
    evidence: dict[str, str] | None = None,
    market: PoolSnapshot | None = None,
    policy: object = GUIDE_STRATEGY,
) -> StrategyPlan:
    candidate = accepted(history, guide_token(earliest_activity_at=NOW - timedelta(hours=5)))
    if evidence:
        candidate = with_evidence(candidate, evidence)
    return StrategyPlanner().plan(
        candidate,
        market or history[-1],
        PORTFOLIO,
        policy,  # type: ignore[arg-type]
    )


def with_evidence(candidate: CandidateDecision, values: dict[str, str]) -> CandidateDecision:
    kept = tuple(item for item in candidate.evidence if item.name not in values)
    added = tuple(EvidenceItem(name=name, value=value) for name, value in values.items())
    return candidate.model_copy(update={"evidence": kept + added})


def ath_history(**current: str | None) -> list[PoolSnapshot]:
    """Seen since creation without holes; the last price 1.3 breaks the pool ATH of 1.2."""

    return guide_history(
        prices=("1", "1.2", "1.1", "1", "1.1", "1.15", "1.3"),
        created_at=NOW - timedelta(minutes=33),
        **current,
    )


def depth_of(result: StrategyPlan, bin_step: int) -> Decimal:
    step = Decimal(1) + Decimal(bin_step) / Decimal(10_000)
    return Decimal(1) - step**result.lower_bin_offset


# --- Spot: one-sided, quote token only ------------------------------------------------------


def test_both_spot_cases_are_one_sided_quote_only_sixty_percent_down() -> None:
    on_ath = plan_for(ath_history())
    overvolume = plan_for(guide_history(short="600000", tvl="1000000"))

    # ceil(ln(1 / 0.4) / ln(1.025)) = 38 bins: 1.025^-38 leaves 39.1% of the price.
    for result, reason in (
        (on_ath, StrategySelectionReason.SPOT_ATH_BREAKOUT),
        (overvolume, StrategySelectionReason.SPOT_OVERVOLUME),
    ):
        assert result.strategy is StrategyKind.SPOT
        assert result.selection_reason is reason
        assert (result.lower_bin_offset, result.upper_bin_offset) == (-38, 0)
        assert Decimal("0.60") <= depth_of(result, 250) < Decimal("0.61")
        assert recorded(result)["range_side"] == "BID_ONLY"
        # Nothing above the price: a rise above the entry bin is the bid-only upside exit,
        # not an instant range breach.
        assert result.exit_policy.upside_breach_grace == timedelta(minutes=30)
        assert result.exit_policy.upside_breach_fraction == Decimal("0.05")
        assert result.blocked_by == ()


# --- Bid-Ask: depth by bin step, from the hour's high ---------------------------------------


@pytest.mark.parametrize(
    ("bin_step", "blocked", "lower_edge"),
    [
        # 1 - 1.005^-69 = 0.2912 (float, rounded): 29% down - far from 70%.
        (50, True, Decimal("0.2912")),
        # 1 - 1.01^-69 = 0.4967: 50% down.
        (100, True, Decimal("0.4967")),
        # 1 - 1.02^-69 = 0.7450: within 70..80%, one bin short of the 75% target.
        (200, False, Decimal("0.7450")),
    ],
)
def test_bid_ask_needs_seventy_percent_within_one_sdk_position(
    bin_step: int, blocked: bool, lower_edge: Decimal
) -> None:
    result = plan_for(guide_history(bin_step=bin_step))

    assert result.strategy is StrategyKind.BID_ASK
    assert (result.lower_bin_offset, result.upper_bin_offset) == (-69, 0)  # 70-bin position
    assert depth_of(result, bin_step).quantize(Decimal("0.0001")) == lower_edge
    assert (StrategyRejection.RANGE_TOO_NARROW in result.blocked_by) is blocked
    assert recorded(result)["range_limited_by_bins"] == "true"


def test_a_too_narrow_bid_ask_gets_no_size_and_no_plan() -> None:
    result = plan_for(guide_history(bin_step=100))

    sized = PositionSizer().size(
        result,
        PORTFOLIO,
        LiquidityState(depth_base_units=10**12, price_impact_cap_base_units=10**12),
        RiskPolicy(ruleset_version=RULESET, config_version=CONFIG),
    )

    assert isinstance(sized, RiskRejection)
    assert [reason.value for reason in sized.reasons] == ["RANGE_TOO_NARROW"]


def test_bid_ask_enters_only_within_ninety_percent_of_the_hours_high() -> None:
    # The hour's high is 1.25; 1.10 is 88% of it, 1.13 is 90.4%.
    below = plan_for(guide_history(prices=("1", "1", "1.25", "1", "1", "1", "1.1")))
    near = plan_for(guide_history(prices=("1", "1", "1.25", "1", "1", "1", "1.13")))

    assert below.strategy is StrategyKind.BID_ASK
    assert below.blocked_by == (StrategyRejection.NOT_NEAR_LOCAL_HIGH,)
    assert below.selection_reason is None
    assert recorded(below)["local_high_price"] == "1.25"
    assert near.blocked_by == ()
    assert near.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH


def test_a_plan_away_from_the_high_is_refused_by_the_sizer_with_its_code() -> None:
    result = plan_for(guide_history(prices=("1", "1", "1.25", "1", "1", "1", "1.1")))

    sized = PositionSizer().size(
        result,
        PORTFOLIO,
        LiquidityState(depth_base_units=10**12, price_impact_cap_base_units=10**12),
        RiskPolicy(ruleset_version=RULESET, config_version=CONFIG),
    )

    assert isinstance(sized, RiskRejection)
    assert [reason.value for reason in sized.reasons] == ["NOT_NEAR_LOCAL_HIGH"]


# --- Spot on an ATH breakout: the token's ATH, high caps only --------------------------------


def priced(history: list[PoolSnapshot], quote_usd: str) -> list[PoolSnapshot]:
    latest = history[-1].model_copy(update={"quote_price_usd": Decimal(quote_usd)})
    return [*history[:-1], latest]


def test_spot_on_ath_reads_the_token_ath_from_gmgn() -> None:
    # Pool price 1.3 SOL x 150 USD = 195 USD.
    at_token_ath = priced(guide_history(prices=("1",) * 6 + ("1.3",)), "150")
    under_token_ath = priced(ath_history(), "150")

    breakout = plan_for(at_token_ath, evidence={"gmgn_token_ath_price": "195"})
    # The pool's own ATH (1.2) is broken, but the token's (250 USD) is not.
    no_breakout = plan_for(under_token_ath, evidence={"gmgn_token_ath_price": "250"})

    assert breakout.selection_reason is StrategySelectionReason.SPOT_ATH_BREAKOUT
    assert recorded(breakout)["ath_source"] == "GMGN_TOKEN"
    assert StrategyFlag.ATH_POOL_ONLY not in breakout.flags
    assert no_breakout.strategy is StrategyKind.BID_ASK
    assert recorded(no_breakout)["ath_source"] == "GMGN_TOKEN"


def test_without_gmgn_the_pool_ath_decides_and_the_plan_says_so() -> None:
    result = plan_for(ath_history())

    assert result.selection_reason is StrategySelectionReason.SPOT_ATH_BREAKOUT
    assert recorded(result)["ath_source"] == "ATH_POOL_ONLY"
    assert StrategyFlag.ATH_POOL_ONLY in result.flags


def test_spot_on_ath_only_on_a_high_cap() -> None:
    small_cap = plan_for(ath_history(market_cap="800000"))
    high_cap = plan_for(ath_history(market_cap="1000000"))

    assert small_cap.strategy is StrategyKind.BID_ASK
    assert high_cap.selection_reason is StrategySelectionReason.SPOT_ATH_BREAKOUT


# --- exits: take profit and holding limits by strategy ----------------------------------------


def test_guide_v1_exits_take_profit_and_holding_limits_by_strategy() -> None:
    bid_ask = plan_for(guide_history(bin_step=200), policy=GUIDE_V1.strategy)
    on_ath = plan_for(ath_history(), policy=GUIDE_V1.strategy)
    overvolume = plan_for(guide_history(short="600000", tvl="1000000"), policy=GUIDE_V1.strategy)

    assert bid_ask.strategy is StrategyKind.BID_ASK
    assert bid_ask.exit_policy.maximum_holding_period == timedelta(hours=24)
    assert bid_ask.exit_policy.take_profit_fraction == Decimal("0.05")
    assert on_ath.exit_policy.maximum_holding_period == timedelta(hours=3)
    assert on_ath.exit_policy.take_profit_fraction == Decimal("0.05")
    assert overvolume.exit_policy.maximum_holding_period == timedelta(minutes=30)
    assert overvolume.exit_policy.take_profit_fraction == Decimal("0.03")


def held(take_profit: str | None = "0.05") -> PositionState:
    return PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.25"),
        upper_price=Decimal("1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=24),
        upside_breach_grace=timedelta(minutes=30),
        upside_breach_fraction=Decimal("0.05"),
        take_profit_fraction=None if take_profit is None else Decimal(take_profit),
    )


def test_take_profit_exits_once_net_pnl_reaches_the_plans_target() -> None:
    market = guide_history(short="400")[-1]
    monitor = PositionMonitor(price_shock_fraction=Decimal("0.80"))

    below = monitor.evaluate(
        held(), market, risk_state(), position_signals(net_pnl_fraction=Decimal("0.0499"))
    )
    reached = monitor.evaluate(
        held(), market, risk_state(), position_signals(net_pnl_fraction=Decimal("0.05"))
    )
    no_target = monitor.evaluate(
        held(None), market, risk_state(), position_signals(net_pnl_fraction=Decimal("0.5"))
    )

    assert below.kind is PositionActionKind.HOLD
    assert reached.kind is PositionActionKind.EXIT
    assert reached.reasons == (PositionReason.TAKE_PROFIT,)
    assert no_target.kind is PositionActionKind.HOLD


def test_the_developer_selling_is_an_exit() -> None:
    market = guide_history(short="400")[-1]

    action = PositionMonitor(price_shock_fraction=Decimal("0.80")).evaluate(
        held(), market, risk_state(), position_signals(developer_sold=True)
    )

    assert action.kind is PositionActionKind.EXIT
    assert action.reasons == (PositionReason.DEV_SOLD,)


def reading(
    *,
    status: DeveloperStatus | None = DeveloperStatus.HOLDING,
    share: str | None = "0.04",
    freshness: Freshness = Freshness.FRESH,
) -> EnhancedTokenSignals:
    return EnhancedTokenSignals(
        provider="gmgn-openapi",
        source_at=NOW,
        freshness=freshness,
        developer_status=status,
        developer_share=None if share is None else Decimal(share),
    )


def test_developer_sold_by_status_or_a_share_more_than_halved() -> None:
    entry = reading()

    assert developer_sold(entry, reading(status=DeveloperStatus.SOLD)) is True
    assert developer_sold(entry, reading(share="0.0199")) is True
    assert developer_sold(entry, reading(share="0.02")) is False
    assert developer_sold(entry, reading()) is False
    # Unknown at entry: the status still counts, the share has no baseline.
    assert developer_sold(None, reading(status=DeveloperStatus.SOLD)) is True
    assert developer_sold(None, reading(share="0")) is False
    # Already sold before the entry: no news.
    assert developer_sold(reading(status=DeveloperStatus.SOLD, share="0"),
                          reading(status=DeveloperStatus.SOLD, share="0")) is False
    # A stale or unavailable reading decides nothing.
    assert developer_sold(entry, reading(status=DeveloperStatus.SOLD,
                                         freshness=Freshness.STALE)) is False
