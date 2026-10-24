"""T19 (spec 6.8, No 10, 39, 40): open positions see the market every 30 s, take profit on
net P&L and leave when the developer sells."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from src.accounting.models import ExitSaleModel
from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.strategy import PositionActionKind
from src.market.models import (
    DataQuality,
    DeveloperStatus,
    EnhancedTokenSignals,
    Freshness,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    ProviderState,
    TokenRiskSnapshot,
    WindowMetrics,
)
from src.portfolio.models import PositionStatus
from src.providers.contracts import ProviderUnavailableError
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.positions import PositionCycle
from tests.domain.guide_fixtures import guide_history
from tests.worker.harness import NOW, POOL, Harness, safe_token


def guide_harness(token: TokenRiskSnapshot | None = None) -> Harness:
    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=20)
    )
    risks = None if token is None else {POOL.base_mint.root: token}
    return Harness(policy=policy, token_risks=risks)


def open_bid_ask(harness: Harness) -> str:
    """A SHADOW Bid-Ask from the local high at bin step 250, entered at ``NOW``."""

    harness.ingest(guide_history())

    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    assert outcome.entry is not None and outcome.entry.position_id is not None, outcome
    return str(outcome.entry.position_id)


def live_reading(
    at: datetime,
    *,
    price: str = "1",
    volume: str | None = None,
    fees_5m: str | None = None,
) -> PoolSnapshot:
    """The pool as ``/pools/{address}`` serves it: the long windows of the entry's market and
    lifetime counters; a 5m window only when the test gives its fees."""

    windows = {
        "30m": WindowMetrics(volume=Decimal("300000"), fees=Decimal("3000")),
        "1h": WindowMetrics(volume=Decimal("900000"), fees=Decimal("8100")),
        "4h": WindowMetrics(volume=Decimal("2700000"), fees=Decimal("24300")),
        "24h": WindowMetrics(volume=Decimal("9000000"), fees=Decimal("81000")),
    }
    if fees_5m is not None:
        windows["5m"] = WindowMetrics(volume=Decimal("100000"), fees=Decimal(fees_5m))
    return PoolSnapshot(
        pool=POOL,
        windows=windows,
        tvl=Decimal("150000"),
        depth=Decimal("75000"),
        price=Decimal(price),
        bin_step=250,
        created_at=NOW - timedelta(hours=6),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        cumulative_volume=None if volume is None else Decimal(volume),
        cumulative_fees=None if volume is None else Decimal(volume) / Decimal(100),
    )


class LiveSource:
    """``MeteoraDataApiProvider.current_snapshot`` with a script of readings."""

    def __init__(self, clock: FrozenClock, **reading: str) -> None:
        self.clock = clock
        self.reading = dict(reading)
        self.requests: list[PoolRef] = []
        self.counter = 0
        self.fail = False

    async def current_snapshot(self, pool: PoolRef) -> PoolSnapshot:
        self.requests.append(pool)
        if self.fail:
            raise ProviderUnavailableError("Data API down")
        values = dict(self.reading)
        if values.pop("counting", None) is not None:
            self.counter += 1_000
            values["volume"] = str(1_000_000 + self.counter)
        return live_reading(self.clock.now(), **values)


class Developer:
    def __init__(self, *readings: EnhancedTokenSignals) -> None:
        self.readings = list(readings)
        self.requests = 0

    @property
    def health(self) -> ProviderHealth:
        return ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def get_token_signals(self, mint: PublicKey) -> EnhancedTokenSignals:
        assert mint == POOL.base_mint
        self.requests += 1
        return self.readings[min(self.requests, len(self.readings)) - 1]


def cycle(harness: Harness, **extra: Any) -> PositionCycle:
    return PositionCycle(
        modes=harness.modes,
        accounting=harness.accounting,
        contexts=harness.contexts,
        history=harness.history,
        onchain=harness.onchain,
        keeper=harness.keeper,
        portfolio=harness.portfolio,
        audit=harness.audit,
        alerts=harness.alerts,
        policy=harness.policy,
        clock=harness.clock,
        metrics=harness.metrics,
        decisions=harness.decisions,
        **extra,
    )


def review_after(harness: Harness, positions: PositionCycle, seconds: int) -> Any:
    harness.clock.advance(timedelta(seconds=seconds))
    (review,) = asyncio.run(positions.run_all()).reviews
    return review


def test_a_crash_between_sweeps_is_seen_by_the_30_second_reread() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    # The last sweep (``NOW``) still says 1.0; the pool fell below the range since.
    crashed = LiveSource(harness.clock, price="0.2")

    review = review_after(harness, cycle(harness, snapshots=crashed), 30)

    assert review.action is PositionActionKind.EXIT and "RANGE_BREACH" in review.reasons
    assert review.closed is True
    assert crashed.requests == [POOL]
    assert harness.modes.open_positions() == ()


def test_rereads_follow_their_own_cadence_not_the_jobs() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    calm = LiveSource(harness.clock)
    positions = cycle(harness, snapshots=calm, snapshot_interval=timedelta(seconds=30))

    for seconds in (30, 15, 15, 15):  # the job every 15 s; rereads at +30, +60
        assert review_after(harness, positions, seconds).action is PositionActionKind.HOLD

    assert len(calm.requests) == 2


def test_a_failed_reread_falls_back_to_the_sweeps_snapshot() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    down = LiveSource(harness.clock)
    down.fail = True

    review = review_after(harness, cycle(harness, snapshots=down), 30)

    assert review.action is PositionActionKind.HOLD
    assert len(down.requests) == 1


def test_the_5m_window_of_rereads_is_summed_over_intervals_by_the_f1_rules() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    # 1 000 traded every 30 s: 10 000 in five minutes - a fifth of the entry's 5m volume of
    # 100 000, below the plan's decay ratio of 0.5. Only readings 4m30 apart make a window.
    counting = LiveSource(harness.clock, counting="yes")
    positions = cycle(harness, snapshots=counting)

    actions = [review_after(harness, positions, 30) for _ in range(10)]

    assert [item.action for item in actions[:9]] == [PositionActionKind.HOLD] * 9
    assert actions[9].action is PositionActionKind.EXIT
    assert "VOLUME_DECAY" in actions[9].reasons


def test_a_sweep_between_rereads_does_not_restart_their_5m_window() -> None:
    """Review B2: a sweep snapshot newer than the last reread carries only the universe's
    readings; the next reread must still sum the 30-second readings (F1)."""

    harness = guide_harness()
    open_bid_ask(harness)
    counting = LiveSource(harness.clock, counting="yes")
    positions = cycle(harness, snapshots=counting)

    actions = [review_after(harness, positions, 30) for _ in range(9)]  # rereads +30 s..+4m30
    # A sweep at +4m45, between the rereads, with the counters as the API reported them then.
    harness.clock.advance(timedelta(seconds=15))
    harness.ingest(
        [live_reading(harness.clock.now(), volume=str(1_000_000 + counting.counter + 500))]
    )
    last = review_after(harness, positions, 15)  # the reread at +5m

    # +5m: 9 000 traded since the reread at +30 s (4m30 back) -> 10 000 in 5 minutes, a
    # fifth of the entry's 5m volume: the plan's volume decay - on time, as without the sweep.
    assert [item.action for item in actions] == [PositionActionKind.HOLD] * 9
    assert last.action is PositionActionKind.EXIT
    assert "VOLUME_DECAY" in last.reasons


def test_a_stay_above_the_range_counts_over_the_rereads() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    # Above the entry bin's top (1.025 at bin step 250), short of the +5% instant exit;
    # the sweep's snapshot meanwhile still says 1.0.
    above = LiveSource(harness.clock, price="1.05")
    positions = cycle(harness, snapshots=above)

    held = [review_after(harness, positions, 30) for _ in range(60)]
    left = review_after(harness, positions, 30)

    # Above since the first reread (+30 s); the 30-minute grace ends at +30 min 30 s.
    assert {item.action for item in held} == {PositionActionKind.HOLD}
    assert left.action is PositionActionKind.EXIT and "RANGE_BREACH" in left.reasons


def test_take_profit_on_the_net_pnl_of_leaving_now() -> None:
    harness = guide_harness()
    position_id = open_bid_ask(harness)
    size = harness.accounting.position_for(position_id).invested_base_units
    # 30 s of a 5m window with 100 000 of fees: 10 000 of the pool's 150 000 TVL = 6.67% of
    # the position; minus 0.5% slippage in and 0.5% out = 5.67% >= the Bid-Ask's 5%.
    rich = LiveSource(harness.clock, fees_5m="100000")

    review = review_after(harness, cycle(harness, snapshots=rich), 30)

    assert review.action is PositionActionKind.EXIT
    assert review.reasons == ("TAKE_PROFIT",)
    assert harness.accounting.position_for(position_id).status is PositionStatus.CLOSED
    (outcome,) = harness.accounting.outcomes()
    assert outcome.valuation.net_pnl_base_units >= size * Decimal("0.05")


def test_no_take_profit_below_the_target() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    # 80 000 of fees: 5.33% - 1% = 4.33% < 5%.
    modest = LiveSource(harness.clock, fees_5m="80000")

    review = review_after(harness, cycle(harness, snapshots=modest), 30)

    assert review.action is PositionActionKind.HOLD


def gmgn(
    *, status: DeveloperStatus = DeveloperStatus.HOLDING, share: str = "0.04"
) -> EnhancedTokenSignals:
    return EnhancedTokenSignals(
        provider="gmgn-openapi",
        source_at=NOW,
        freshness=Freshness.FRESH,
        developer_status=status,
        developer_share=Decimal(share),
    )


def test_the_developer_selling_closes_the_position_and_gmgn_is_asked_every_5_minutes() -> None:
    harness = guide_harness()
    open_bid_ask(harness)
    developer = Developer(gmgn(), gmgn(status=DeveloperStatus.SOLD))
    positions = cycle(harness, developer=developer)

    first = review_after(harness, positions, 15)
    cached = review_after(harness, positions, 15)
    sold = review_after(harness, positions, 5 * 60)

    assert first.action is cached.action is PositionActionKind.HOLD
    assert sold.action is PositionActionKind.EXIT and "DEV_SOLD" in sold.reasons
    assert developer.requests == 2


def test_a_developer_share_halved_since_the_entry_is_a_sale() -> None:
    entry = safe_token().model_copy(update={"enhanced": gmgn(share="0.04")})
    harness = guide_harness(entry)
    open_bid_ask(harness)
    developer = Developer(gmgn(share="0.019"))

    review = review_after(harness, cycle(harness, developer=developer), 15)

    assert review.action is PositionActionKind.EXIT and "DEV_SOLD" in review.reasons


def test_without_the_sale_setting_a_shadow_exit_keeps_the_returned_token() -> None:
    harness = guide_harness()
    position_id = open_bid_ask(harness)
    fallen = LiveSource(harness.clock, price="0.2")
    positions = cycle(harness, snapshots=fallen, exit_sale=ExitSaleModel(enabled=False))

    review_after(harness, positions, 30)

    kinds = [entry.kind.value for entry in harness.accounting.ledger_entries()]
    assert kinds == ["OPEN", "CLOSE"]  # no modeled SWAP
    assert harness.accounting.position_for(position_id).status is PositionStatus.CLOSED
