"""T19 entries (spec 6.8): one-sided Spot from a SOL-only wallet, liquidity slippage as a
share of the range (``ACTIVE_BIN_DRIFT``)."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.primitives import BaseUnits, PublicKey
from src.domain.models.trading import TradingMode
from src.domain.strategy import StrategyKind, StrategySelectionReason
from src.execution.contracts import DlmmCommandV2, DlmmOperation, LiquidityStrategy
from src.market.models import DataQuality, PoolSnapshot, WalletState
from src.modes import PendingPlanStatus
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import guide_history, guide_snapshot
from tests.worker.harness import NOW, POOL, memory_repositories
from tests.worker.test_gateway_execution import (
    ANCHOR,
    WALLET_ADDRESS,
    FakeExecutor,
    StaticPoolStates,
    approve,
    ingest,
    plan_status,
    promote,
    run_job,
    worker,
)

SOL_ONLY = WalletState(
    wallet=PublicKey(WALLET_ADDRESS),
    native_balance=BaseUnits(10**15),
    token_balances={},
    observed_at=NOW,
    source_at=NOW,
    ingested_at=NOW,
    data_quality=DataQuality.COMPLETE,
)


def approval_runtime(
    history: list[PoolSnapshot], states: StaticPoolStates, **settings: Any
) -> tuple[Any, FakeExecutor, Any]:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.APPROVAL)
    ingest(repos, *history)
    policy = guide_policy(
        GuidePolicySettings(
            baseline_points=6, minimum_baseline_points=6, maximum_history_points=20, **settings
        )
    )
    runtime = worker(
        repos, clock, executor, TradingMode.APPROVAL, policy=policy, pool_states=states,
        wallet=SOL_ONLY,
    )
    return runtime, executor, repos


def enter(runtime: Any) -> Any:
    async def run() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    return asyncio.run(run())


def approval_entry(
    history: list[PoolSnapshot], states: StaticPoolStates, **settings: Any
) -> tuple[Any, FakeExecutor, Any]:
    runtime, executor, repos = approval_runtime(history, states, **settings)
    return enter(runtime), executor, repos


def test_an_overvolume_spot_enters_with_quote_only_from_a_sol_only_wallet() -> None:
    """Seam StrategyPlanner.plan -> EntryExecutor: the author's Spot is one-sided, so the
    executor gets a Spot of Y alone from the active bin down and needs no base token."""

    outcome, executor, repos = approval_entry(
        guide_history(short="600000", tvl="1000000"),
        StaticPoolStates(ANCHOR.active_bin_id, bin_step=250),
    )

    plan = repos.decisions.plans[outcome.plan_id].sized_plan.plan
    assert plan.strategy is StrategyKind.SPOT
    assert plan.selection_reason is StrategySelectionReason.SPOT_OVERVOLUME
    assert outcome.entry is not None and outcome.entry.reason == "AWAITING_APPROVAL"
    ((command, _),) = executor.simulated
    assert isinstance(command, DlmmCommandV2)
    assert command.strategy is LiquidityStrategy.SPOT
    assert command.amount_x == "0" and int(command.amount_y or "0") > 0
    # 38 bins below the active one (60% down at bin step 250), none above.
    assert (command.min_bin_id, command.max_bin_id) == (
        ANCHOR.active_bin_id - 38,
        ANCHOR.active_bin_id,
    )


@pytest.mark.parametrize(("drift", "admitted"), [(17, True), (-17, True), (18, False)])
def test_the_active_bin_may_move_thirty_percent_of_the_range(drift: int, admitted: bool) -> None:
    # Bid-Ask at bin step 250: 57 bins below plus the active one = 58; 30% of 58 = 17.4 -> 17.
    outcome, executor, repos = approval_entry(
        guide_history(), StaticPoolStates(ANCHOR.active_bin_id + drift, bin_step=250)
    )

    assert outcome.entry is not None
    if admitted:
        ((command, _),) = executor.simulated
        assert isinstance(command, DlmmCommandV2)
        assert command.active_bin_tolerance == 17
        assert command.active_bin_id == ANCHOR.active_bin_id + drift
    else:
        assert outcome.entry.reason == "ACTIVE_BIN_DRIFT"
        assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.CANCELLED
        assert executor.simulated == []


def test_a_drifted_approval_entry_is_tracked_from_the_bin_it_was_opened_at() -> None:
    """Review B1: the command is anchored on the freshly read active bin, 17 bins (within
    30% of 58) above the sweep's; the position's range must follow it, so the price at that
    bin is the entry - not a breach above the range on the first review."""

    runtime, executor, repos = approval_runtime(
        guide_history(), StaticPoolStates(ANCHOR.active_bin_id + 17, bin_step=250)
    )
    outcome = enter(runtime)
    assert outcome.entry is not None and outcome.entry.reason == "AWAITING_APPROVAL"
    ((command, _),) = executor.simulated
    approve(runtime, command.command_id, timedelta(seconds=60))
    run_job(runtime, "candidate")  # the approved entry is executed and booked
    (position_id,) = repos.mode.open_positions()
    # The sweep's price was 1.0; 17 bins of 2.5% up is where the pool now trades.
    moved = Decimal("1.025") ** 17
    runtime.clock.advance(timedelta(minutes=1))
    ingest(repos, guide_snapshot(runtime.clock.now(), price=str(moved)))

    run_job(runtime, "position")

    proposed = [
        event for event in repos.audit.events
        if event.event_type is EventType.POSITION_ACTION_PROPOSED
    ]
    assert [event.payload["action"] for event in proposed] == ["HOLD"]
    assert repos.mode.open_positions() == (position_id,)
    assert [item.operation for item, _ in executor.simulated] == [DlmmOperation.OPEN]
    context = asyncio.run(repos.positions.get(position_id))
    assert context is not None and context.entry_price == moved
    assert context.upper_price == moved


def test_the_share_of_the_range_is_a_setting() -> None:
    # 10% of 58 bins = 5: a drift of 6 is refused, and the command carries 5.
    refused, _, _ = approval_entry(
        guide_history(),
        StaticPoolStates(ANCHOR.active_bin_id + 6, bin_step=250),
        liquidity_slippage="0.10",
    )
    sent, executor, _ = approval_entry(
        guide_history(),
        StaticPoolStates(ANCHOR.active_bin_id + 5, bin_step=250),
        liquidity_slippage="0.10",
    )

    assert refused.entry is not None and refused.entry.reason == "ACTIVE_BIN_DRIFT"
    ((command, _),) = executor.simulated
    assert isinstance(command, DlmmCommandV2) and command.active_bin_tolerance == 5
    assert sent.entry is not None and sent.entry.reason == "AWAITING_APPROVAL"
