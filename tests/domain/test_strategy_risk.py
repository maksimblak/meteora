from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.domain.features import SignalReadiness, VolumeSpikeFeatures
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.risk import (
    LiquidityState,
    PortfolioState,
    PositionSizer,
    PromotionEvidence,
    PromotionPolicy,
    RiskDecisionStatus,
    RiskEngine,
    RiskPolicy,
    RiskReason,
    RiskRejection,
    RiskState,
    SizedPlan,
)
from src.domain.screening import CandidateDecision, CandidateStatus
from src.domain.strategy.models import (
    Direction,
    PositionActionKind,
    PositionReason,
    PositionSignals,
    PositionState,
    StrategyKind,
    StrategyPlan,
    StrategyPolicy,
)
from src.domain.strategy.planner import PositionMonitor, StrategyPlanner
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    RiskSignal,
    WindowMetrics,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("11111111111111111111111111111111"),
    quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
)


def market(*, price: str = "1", short_volume: str = "400") -> PoolSnapshot:
    return PoolSnapshot(
        pool=POOL,
        windows={
            "5m": WindowMetrics(volume=Decimal(short_volume), fees=Decimal("2")),
            "30m": WindowMetrics(volume=Decimal("1200"), fees=Decimal("20")),
            "1h": WindowMetrics(volume=Decimal("2400"), fees=Decimal("40")),
            "4h": WindowMetrics(volume=Decimal("9600"), fees=Decimal("160")),
            "24h": WindowMetrics(volume=Decimal("57600"), fees=Decimal("960")),
        },
        tvl=Decimal("1000"),
        depth=Decimal("500"),
        price=Decimal(price),
        bin_step=25,
        created_at=NOW - timedelta(days=7),
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def candidate(*, momentum: str = "0", volatility: str = "0.02") -> CandidateDecision:
    features = VolumeSpikeFeatures(
        decision_at=NOW,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        pool_address=POOL.address,
        base_mint=POOL.base_mint,
        quote_mint=POOL.quote_mint,
        baseline_points=6,
        readiness=SignalReadiness.READY,
        current_short_volume=Decimal("400"),
        current_long_volume=Decimal("1200"),
        short_median=Decimal("100"),
        long_median=Decimal("600"),
        short_ratio=Decimal("4"),
        long_ratio=Decimal("2"),
        robust_z_score=Decimal("10"),
        fee_tvl=Decimal("0.02"),
        tvl_change=Decimal("0"),
        price_volatility=Decimal(volatility),
        price_momentum=Decimal(momentum),
        turnover=Decimal("1.2"),
        transaction_count=100,
        unique_participants=20,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        missing_signals=(),
        ruleset_version="rules-1",
        config_version="config-1",
    )
    return CandidateDecision(
        status=CandidateStatus.ACCEPT,
        pool_address=POOL.address,
        token_mint=POOL.base_mint,
        decision_at=NOW,
        mode=TradingMode.LIVE,
        estimated_price_impact=Decimal("0.01"),
        score=Decimal("0.9"),
        reasons=(),
        missing_signals=(),
        evidence=(),
        score_components=(),
        features=features,
        ruleset_version="rules-1",
        config_version="config-1",
        correlation_id="cycle-0001",
    )


def strategy_policy(**updates: object) -> StrategyPolicy:
    values: dict[str, object] = {
        "ruleset_version": "rules-1",
        "config_version": "config-1",
    }
    values.update(updates)
    return StrategyPolicy(**values)  # type: ignore[arg-type]


def risk_policy(**updates: object) -> RiskPolicy:
    values: dict[str, object] = {
        "ruleset_version": "rules-1",
        "config_version": "config-1",
    }
    values.update(updates)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def plan(*, momentum: str = "0", volatility: str = "0.02") -> StrategyPlan:
    return StrategyPlanner().plan(
        candidate(momentum=momentum, volatility=volatility),
        market(),
        PortfolioState(
            equity_base_units=100_000,
            available_balance_base_units=100_000,
            open_position_count=0,
        ),
        strategy_policy(),
    )


def sized_plan() -> SizedPlan:
    result = PositionSizer().size(
        plan(),
        PortfolioState(
            equity_base_units=100_000,
            available_balance_base_units=100_000,
            open_position_count=0,
        ),
        LiquidityState(depth_base_units=1_000_000, price_impact_cap_base_units=100_000),
        risk_policy(),
    )
    assert isinstance(result, SizedPlan)
    return result


def risk_state(
    *,
    positions: int = 0,
    realized: int = 0,
    unrealized: int = 0,
    kill: bool = False,
    as_of: datetime = NOW + timedelta(minutes=1),
) -> RiskState:
    return RiskState(
        as_of=as_of,
        risk_day_start=NOW.replace(hour=0),
        start_of_day_equity_base_units=100_000,
        realized_pnl_base_units=realized,
        conservative_unrealized_pnl_base_units=unrealized,
        open_position_count=positions,
        kill_switch_active=kill,
    )


def position_signals(**updates: object) -> PositionSignals:
    values: dict[str, object] = {
        "current_fee_tvl": Decimal("0.02"),
        "baseline_fee_tvl": Decimal("0.02"),
        "competing_tvl_growth": Decimal("0.10"),
        "normalized_range_position": Decimal("0.50"),
        "token_risk": RiskSignal.SAFE,
        "estimated_rebalance_cost_base_units": 10,
        "estimated_close_cost_base_units": 5,
        "expected_remaining_fee_income_base_units": 100,
    }
    values.update(updates)
    return PositionSignals(**values)  # type: ignore[arg-type]


def test_spot_is_neutral_and_bid_ask_requires_confirmed_direction() -> None:
    neutral = plan(momentum="0.009")
    bullish = plan(momentum="0.01")
    bearish = plan(momentum="-0.01")

    assert neutral.strategy is StrategyKind.SPOT
    assert neutral.direction is Direction.NEUTRAL
    assert neutral.lower_bin_offset == -neutral.upper_bin_offset
    assert bullish.strategy is StrategyKind.BID_ASK
    assert bullish.direction is Direction.BULLISH
    assert bullish.lower_bin_offset == -1
    assert bearish.direction is Direction.BEARISH
    assert bearish.upper_bin_offset == 1


def test_range_and_plan_expiry_are_clamped_and_deterministic() -> None:
    wide = StrategyPlanner().plan(
        candidate(volatility="0.90"),
        market(),
        PortfolioState(
            equity_base_units=100_000,
            available_balance_base_units=100_000,
            open_position_count=0,
        ),
        strategy_policy(maximum_bins=12),
    )

    assert wide.upper_bin_offset == 12
    assert wide.created_at == NOW
    assert wide.expires_at == NOW + timedelta(minutes=5)
    assert wide.loss_fraction_at_invalidation == Decimal("0.50")


def test_bin_range_expands_with_horizon_and_stricter_exit_probability() -> None:
    planner = StrategyPlanner()
    portfolio = PortfolioState(
        equity_base_units=100_000,
        available_balance_base_units=100_000,
        open_position_count=0,
    )
    short = planner.plan(
        candidate(volatility="0.002"),
        market(),
        portfolio,
        strategy_policy(holding_horizon=timedelta(hours=1)),
    )
    long = planner.plan(
        candidate(volatility="0.002"),
        market(),
        portfolio,
        strategy_policy(holding_horizon=timedelta(hours=16)),
    )
    strict = planner.plan(
        candidate(volatility="0.002"),
        market(),
        portfolio,
        strategy_policy(
            holding_horizon=timedelta(hours=1),
            maximum_range_exit_probability=Decimal("0.025"),
        ),
    )

    assert short.upper_bin_offset < long.upper_bin_offset
    assert short.upper_bin_offset < strict.upper_bin_offset
    assert strict.range_exit_probability == Decimal("0.025")


@pytest.mark.parametrize(
    ("equity", "loss", "depth", "balance"),
    [
        (100_000, "0.05", 1_000_000, 100_000),
        (1, "1", 100, 1),
        (999_999, "0.33", 700_000, 50_000),
        (10_000_000, "0.001", 20_000, 8_000_000),
        (55_555, "0.50", 999_999, 55_555),
    ],
)
def test_sizing_property_never_exceeds_10_percent_or_1_percent_risk(
    equity: int,
    loss: str,
    depth: int,
    balance: int,
) -> None:
    raw_plan = plan().model_copy(update={"loss_fraction_at_invalidation": Decimal(loss)})
    result = PositionSizer().size(
        raw_plan,
        PortfolioState(
            equity_base_units=equity,
            available_balance_base_units=balance,
            open_position_count=0,
        ),
        LiquidityState(depth_base_units=depth, price_impact_cap_base_units=depth),
        risk_policy(),
    )

    if isinstance(result, RiskRejection):
        assert RiskReason.ZERO_CAPACITY in result.reasons
    else:
        assert result.size_base_units <= int(Decimal(equity) * Decimal("0.10"))
        assert Decimal(result.size_base_units) * Decimal(loss) <= Decimal(equity) * Decimal("0.01")
        assert result.size_base_units <= balance


def test_sizing_uses_minimum_of_all_caps_and_names_binding_cap() -> None:
    result = PositionSizer().size(
        plan(),
        PortfolioState(
            equity_base_units=100_000,
            available_balance_base_units=1_500,
            open_position_count=0,
        ),
        LiquidityState(depth_base_units=1_000_000, price_impact_cap_base_units=100_000),
        risk_policy(fee_reserve_base_units=500),
    )

    assert isinstance(result, SizedPlan)
    assert result.size_base_units == 1_000
    assert result.binding_caps == (RiskReason.BALANCE_CAP,)


def test_estimated_loss_rounds_up_at_smallest_base_unit() -> None:
    tiny_plan = plan().model_copy(update={"loss_fraction_at_invalidation": Decimal("0.01")})
    result = PositionSizer().size(
        tiny_plan,
        PortfolioState(
            equity_base_units=1_000,
            available_balance_base_units=1,
            open_position_count=0,
        ),
        LiquidityState(depth_base_units=100, price_impact_cap_base_units=1),
        risk_policy(),
    )

    assert isinstance(result, SizedPlan)
    assert result.size_base_units == 1
    assert result.estimated_loss_base_units == 1
    assert result.basis.risk_budget_base_units == 10


def test_forged_sized_plan_is_rejected_even_when_model_validation_is_bypassed() -> None:
    valid = sized_plan()
    with pytest.raises(ValidationError, match="exceeds a sizing cap"):
        SizedPlan(
            plan=valid.plan,
            size_base_units=valid.caps.allocation_base_units + 1,
            estimated_loss_base_units=valid.estimated_loss_base_units,
            caps=valid.caps,
            basis=valid.basis,
            binding_caps=valid.binding_caps,
            ruleset_version=valid.ruleset_version,
            config_version=valid.config_version,
            correlation_id=valid.correlation_id,
        )
    forged = SizedPlan.model_construct(
        **{
            **valid.model_dump(),
            "plan": valid.plan,
            "caps": valid.caps,
            "basis": valid.basis,
            "size_base_units": valid.caps.allocation_base_units + 1,
            "estimated_loss_base_units": 1,
        }
    )

    decision = RiskEngine(risk_policy()).pre_trade(forged, risk_state())

    assert decision.status is RiskDecisionStatus.REJECT
    assert RiskReason.SIZED_PLAN_INVARIANT_BREACH in decision.reasons


def test_fourth_position_and_exact_daily_stop_are_rejected_before_execution() -> None:
    engine = RiskEngine(risk_policy())

    fourth = engine.pre_trade(sized_plan(), risk_state(positions=3))
    stopped = engine.pre_trade(sized_plan(), risk_state(realized=-4_000))
    allowed = engine.pre_trade(sized_plan(), risk_state(positions=2, realized=-3_999))

    assert fourth.status is RiskDecisionStatus.REJECT
    assert fourth.reasons == (RiskReason.POSITION_LIMIT_REACHED,)
    assert stopped.status is RiskDecisionStatus.REJECT
    assert stopped.reasons == (RiskReason.DAILY_STOP_REACHED,)
    assert allowed.status is RiskDecisionStatus.APPROVE


def test_positive_unrealized_does_not_hide_realized_daily_loss() -> None:
    decision = RiskEngine(risk_policy()).pre_trade(
        sized_plan(), risk_state(realized=-4_000, unrealized=10_000)
    )

    assert decision.status is RiskDecisionStatus.REJECT
    assert RiskReason.DAILY_STOP_REACHED in decision.reasons


def test_expired_plan_and_kill_switch_fail_closed() -> None:
    decision = RiskEngine(risk_policy()).pre_trade(
        sized_plan(), risk_state(kill=True, as_of=NOW + timedelta(minutes=5))
    )

    assert decision.status is RiskDecisionStatus.REJECT
    assert decision.reasons == (
        RiskReason.KILL_SWITCH_ACTIVE,
        RiskReason.PLAN_EXPIRED,
    )


def test_position_monitor_exits_on_decay_or_range_and_alerts_on_kill_switch() -> None:
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.9"),
        upper_price=Decimal("1.1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
    )

    exit_action = PositionMonitor().evaluate(
        position,
        market(price="1.11", short_volume="100"),
        risk_state(),
        position_signals(),
    )
    alert_action = PositionMonitor().evaluate(
        position,
        market(),
        risk_state(kill=True),
        position_signals(),
    )

    assert exit_action.kind is PositionActionKind.EXIT
    assert exit_action.reasons == (
        PositionReason.RANGE_BREACH,
        PositionReason.VOLUME_DECAY,
        PositionReason.PRICE_SHOCK,
    )
    assert alert_action.kind is PositionActionKind.ALERT
    assert alert_action.reasons == (PositionReason.KILL_SWITCH_ACTIVE,)


def test_position_monitor_reduces_near_edge_only_when_rebalance_wins_after_costs() -> None:
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.9"),
        upper_price=Decimal("1.1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
    )

    reduce = PositionMonitor().evaluate(
        position,
        market(),
        risk_state(),
        position_signals(normalized_range_position=Decimal("0.10")),
    )
    close = PositionMonitor().evaluate(
        position,
        market(),
        risk_state(),
        position_signals(
            normalized_range_position=Decimal("0.90"),
            estimated_rebalance_cost_base_units=200,
            expected_remaining_fee_income_base_units=0,
        ),
    )

    assert reduce.kind is PositionActionKind.REDUCE
    assert reduce.reasons == (
        PositionReason.NEAR_RANGE_EDGE,
        PositionReason.REBALANCE_AFTER_COSTS,
    )
    assert close.kind is PositionActionKind.EXIT
    assert PositionReason.CLOSE_BEATS_REBALANCE in close.reasons


def test_position_monitor_covers_fee_tvl_competition_and_token_deterioration() -> None:
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.9"),
        upper_price=Decimal("1.1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
    )

    action = PositionMonitor().evaluate(
        position,
        market(),
        risk_state(),
        position_signals(
            current_fee_tvl=Decimal("0.009"),
            competing_tvl_growth=Decimal("0.51"),
            token_risk=RiskSignal.UNSAFE,
        ),
    )

    assert action.kind is PositionActionKind.EXIT
    assert action.reasons == (
        PositionReason.TOKEN_RISK_DETERIORATED,
        PositionReason.FEE_DECAY,
        PositionReason.TVL_COMPETITION,
    )


@pytest.mark.parametrize(
    ("price", "short_volume", "as_of", "token_risk", "expected_reason"),
    [
        (
            "1",
            "400",
            NOW + timedelta(minutes=1),
            RiskSignal.UNSAFE,
            PositionReason.TOKEN_RISK_DETERIORATED,
        ),
        ("1.11", "400", NOW + timedelta(minutes=1), RiskSignal.SAFE, PositionReason.RANGE_BREACH),
        ("1", "400", NOW + timedelta(hours=5), RiskSignal.SAFE, PositionReason.MAX_HOLDING_PERIOD),
        ("1", "100", NOW + timedelta(minutes=1), RiskSignal.SAFE, PositionReason.VOLUME_DECAY),
    ],
)
def test_confirmed_exit_is_not_masked_by_missing_fee_signal(
    price: str,
    short_volume: str,
    as_of: datetime,
    token_risk: RiskSignal,
    expected_reason: PositionReason,
) -> None:
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.9"),
        upper_price=Decimal("1.1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
    )

    action = PositionMonitor().evaluate(
        position,
        market(price=price, short_volume=short_volume),
        risk_state(as_of=as_of),
        position_signals(current_fee_tvl=None, token_risk=token_risk),
    )

    assert action.kind is PositionActionKind.EXIT
    assert expected_reason in action.reasons
    assert PositionReason.FEE_SIGNAL_UNKNOWN in action.reasons


def test_position_monitor_aggregates_alerts_only_when_no_stronger_action_exists() -> None:
    position = PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("1"),
        lower_price=Decimal("0.9"),
        upper_price=Decimal("1.1"),
        entry_short_volume=Decimal("400"),
        maximum_holding_period=timedelta(hours=4),
    )

    action = PositionMonitor().evaluate(
        position,
        market().model_copy(update={"freshness": Freshness.STALE}),
        risk_state(kill=True),
        position_signals(current_fee_tvl=None, token_risk=RiskSignal.UNKNOWN),
    )

    assert action.kind is PositionActionKind.ALERT
    assert action.reasons == (
        PositionReason.KILL_SWITCH_ACTIVE,
        PositionReason.MARKET_DATA_UNSAFE,
        PositionReason.TOKEN_RISK_UNKNOWN,
        PositionReason.FEE_SIGNAL_UNKNOWN,
    )


def test_risk_config_cannot_relax_user_caps() -> None:
    with pytest.raises(ValidationError, match="allocation fraction"):
        risk_policy(maximum_allocation_fraction=Decimal("0.100001"))
    with pytest.raises(ValidationError, match="position risk fraction"):
        risk_policy(maximum_position_risk_fraction=Decimal("0.010001"))
    with pytest.raises(ValidationError, match="maximum_open_positions"):
        risk_policy(maximum_open_positions=4)
    with pytest.raises(ValidationError, match="daily stop fraction"):
        risk_policy(daily_stop_fraction=Decimal("0.040001"))


def test_promotion_policy_requires_evidence_and_operator_confirmation() -> None:
    decision = PromotionPolicy().evaluate(
        TradingMode.SHADOW,
        PromotionEvidence(
            full_shadow_days=7,
            completed_positions=20,
            net_expectancy_positive=True,
            maximum_drawdown=Decimal("0.04"),
            data_coverage=Decimal("0.99"),
            open_critical_alerts=0,
            operator_confirmed=True,
        ),
    )

    assert decision.allowed is True
    assert decision.reasons == ()
