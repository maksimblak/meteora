"""PositionMonitor on live snapshots: derived 5m windows, the plan's own decay ratio."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.domain.models.primitives import PublicKey
from src.domain.risk.models import RiskState
from src.domain.strategy import (
    PositionActionKind,
    PositionMonitor,
    PositionReason,
    PositionSignals,
    PositionState,
)
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    RiskSignal,
    ShortWindowDerivation,
    ShortWindowStatus,
    WindowMetrics,
    WindowOrigin,
)

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"),
    base_mint=PublicKey("So11111111111111111111111111111111111111112"),
    quote_mint=PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
)


def live_market(short: str, *, price: str = "100") -> PoolSnapshot:
    """What the Data API gives after derivation: no depth, a 5m window from the counters."""

    windows = {
        name: WindowMetrics(volume=Decimal("60000"), fees=Decimal("30"), protocol_fees=Decimal("3"))
        for name in ("30m", "1h", "4h", "24h")
    }
    windows["5m"] = WindowMetrics(volume=Decimal(short), fees=Decimal("1"))
    return PoolSnapshot(
        pool=POOL,
        windows=windows,
        tvl=Decimal("4000000"),
        depth=None,
        price=Decimal(price),
        bin_step=4,
        created_at=NOW - timedelta(days=300),
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        provider="meteora_data_api",
        short_window_source="30m",
        cumulative_volume=Decimal("26439418383"),
        cumulative_fees=Decimal("11573615"),
        short_window=ShortWindowDerivation(
            window="5m",
            origin=WindowOrigin.CUMULATIVE_DELTA,
            status=ShortWindowStatus.DERIVED,
            previous_source_at=NOW - timedelta(minutes=5),
            interval_seconds=Decimal(300),
        ),
    )


def position(*, ratio: Decimal | None) -> PositionState:
    return PositionState(
        position_id="position-1",
        opened_at=NOW - timedelta(minutes=10),
        entry_price=Decimal("100"),
        lower_price=Decimal("90"),
        upper_price=Decimal("110"),
        entry_short_volume=Decimal("100000"),
        maximum_holding_period=timedelta(minutes=30),
        volume_decay_ratio=ratio,
    )


def risk() -> RiskState:
    return RiskState(
        as_of=NOW,
        risk_day_start=NOW.replace(hour=0),
        start_of_day_equity_base_units=1_000_000,
        realized_pnl_base_units=0,
        conservative_unrealized_pnl_base_units=0,
        open_position_count=1,
        kill_switch_active=False,
    )


def signals(*, position: str = "0.5", above_since: datetime | None = None) -> PositionSignals:
    return PositionSignals(
        current_fee_tvl=Decimal("0.01"),
        baseline_fee_tvl=Decimal("0.01"),
        competing_tvl_growth=Decimal("0"),
        normalized_range_position=Decimal(position),
        token_risk=RiskSignal.SAFE,
        estimated_rebalance_cost_base_units=10,
        estimated_close_cost_base_units=5,
        expected_remaining_fee_income_base_units=0,
        above_range_since=above_since,
    )


def bid_only() -> PositionState:
    """A Bid-Ask from the high: the range ends at the entry price's active bin (D08)."""

    return PositionState(
        position_id="position-2",
        opened_at=NOW - timedelta(hours=1),
        entry_price=Decimal("100"),
        lower_price=Decimal("25"),
        upper_price=Decimal("100"),
        entry_short_volume=Decimal("1000"),
        maximum_holding_period=timedelta(hours=4),
        upside_breach_grace=timedelta(minutes=30),
        upside_breach_fraction=Decimal("0.05"),
    )


def test_a_bid_only_range_is_not_left_on_a_tick_or_a_bin_above_it() -> None:
    monitor = PositionMonitor()
    state = bid_only()
    # bin step 4 bps: the entry bin ends at 100.04.
    inside_bin = monitor.evaluate(
        state, live_market("1000", price="100.001"), risk(), signals(position="1")
    )
    one_bin_up = monitor.evaluate(
        state, live_market("1000", price="100.05"), risk(), signals(position="1", above_since=NOW)
    )

    assert inside_bin.kind is PositionActionKind.HOLD
    assert one_bin_up.kind is PositionActionKind.HOLD
    assert PositionMonitor.above_range(state, live_market("1000", price="100.001")) is False
    assert PositionMonitor.above_range(state, live_market("1000", price="100.05")) is True


def test_a_bid_only_range_exits_once_the_price_stays_above_or_runs_away() -> None:
    monitor = PositionMonitor()
    state = bid_only()

    stayed = monitor.evaluate(
        state,
        live_market("1000", price="100.05"),
        risk(),
        signals(position="1", above_since=NOW - timedelta(minutes=31)),
    )
    ran_away = monitor.evaluate(  # 5% above the entry bin's top (100.04 * 1.05 = 105.042)
        state, live_market("1000", price="105.05"), risk(), signals(position="1", above_since=NOW)
    )
    fell_through = monitor.evaluate(
        state, live_market("1000", price="24"), risk(), signals(position="0")
    )

    assert (stayed.kind, stayed.reasons) == (
        PositionActionKind.EXIT,
        (PositionReason.RANGE_BREACH,),
    )
    assert (ran_away.kind, ran_away.reasons) == (
        PositionActionKind.EXIT,
        (PositionReason.RANGE_BREACH,),
    )
    assert fell_through.kind is PositionActionKind.EXIT
    assert PositionReason.RANGE_BREACH in fell_through.reasons


def test_a_complete_live_snapshot_raises_no_market_data_alert() -> None:
    action = PositionMonitor().evaluate(
        position(ratio=None), live_market("100000"), risk(), signals()
    )

    assert action.kind is PositionActionKind.HOLD
    assert PositionReason.MARKET_DATA_UNSAFE not in action.reasons


def test_the_overvolume_spot_exits_on_the_first_drop_by_its_own_plan_ratio() -> None:
    # The monitor's general ratio is 0.5; the overvolume plan carries 1 (first drop).
    monitor = PositionMonitor(volume_decay_ratio=Decimal("0.5"))

    general = monitor.evaluate(position(ratio=None), live_market("95000"), risk(), signals())
    overvolume = monitor.evaluate(
        position(ratio=Decimal("1")), live_market("95000"), risk(), signals()
    )

    assert general.kind is PositionActionKind.HOLD
    assert overvolume.kind is PositionActionKind.EXIT
    assert overvolume.reasons == (PositionReason.VOLUME_DECAY,)
