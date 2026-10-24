"""F4: a non-SHADOW entry is booked at one price on every path and re-checks "from the
highs" on the fresh active bin (spec 6.8, D10).

The executor is the in-process contract V2 fake of ``tests/worker/test_gateway_execution``.
"""

import asyncio
from decimal import Decimal

import pytest

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.modes import PendingPlanStatus
from tests.domain.guide_fixtures import guide_history
from tests.worker.harness import NOW, memory_repositories, spike_history
from tests.worker.test_gateway_execution import (
    ANCHOR,
    FakeExecutor,
    StaticPoolStates,
    ingest,
    plan_status,
    promote,
    run_job,
    worker,
)
from tests.worker.test_t19_entries import approval_entry

# --- (T19) "from the highs" holds at the price the executor will actually open at ---------


@pytest.mark.parametrize(
    ("drift", "reason"),
    [
        # The hour's high is 1.0 (every price of the history), the rule 90% of it. At bin step
        # 250 the fresh price is 1.025**drift: -4 -> 0.90595 >= 0.9; -5 -> 0.88385 < 0.9.
        (-4, "AWAITING_APPROVAL"),
        (-5, "NOT_NEAR_LOCAL_HIGH"),
        (-17, "NOT_NEAR_LOCAL_HIGH"),  # within the 30% liquidity slippage, 34% below the plan
        (17, "AWAITING_APPROVAL"),  # above the high is still "from the highs"
    ],
)
def test_a_bid_ask_from_the_high_is_rechecked_on_the_fresh_active_bin(
    drift: int, reason: str
) -> None:
    outcome, executor, repos = approval_entry(
        guide_history(), StaticPoolStates(ANCHOR.active_bin_id + drift, bin_step=250)
    )

    assert outcome.entry is not None and outcome.entry.reason == reason
    if reason == "NOT_NEAR_LOCAL_HIGH":
        assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.CANCELLED
        assert executor.simulated == []
    else:
        assert len(executor.simulated) == 1


def test_a_spot_entry_has_no_high_to_recheck() -> None:
    # An overvolume Spot (39 bins, 11 may move) five bins below the sweep is still entered.
    outcome, executor, _ = approval_entry(
        guide_history(short="600000", tvl="1000000"),
        StaticPoolStates(ANCHOR.active_bin_id - 5, bin_step=250),
    )

    assert outcome.entry is not None and outcome.entry.reason == "AWAITING_APPROVAL"
    assert len(executor.simulated) == 1


# --- (T19) one entry price: the immediate LIVE confirmation books the context's price -----


def test_an_immediately_confirmed_live_entry_is_booked_at_the_context_entry_price() -> None:
    # The fresh active bin is 2 bins above the sweep's (tolerance 3): the executor opens
    # around it, so the entry is the sweep's price 1 moved two bin steps of 25 bps up.
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    runtime = worker(
        repos, clock, executor, TradingMode.LIVE,
        pool_states=StaticPoolStates(ANCHOR.active_bin_id + 2),
    )

    run_job(runtime, "candidate")

    (position_id,) = repos.mode.open_positions()
    context = asyncio.run(repos.positions.get(position_id))
    (entry,) = repos.accounting.ledger_entries()
    expected = Decimal("1.0025") ** 2  # 1.00500625
    assert context is not None and context.entry_price == expected
    assert entry.kind.value == "OPEN" and entry.price == expected
    position = repos.accounting.position_for(position_id)
    assert position is not None
    assert {tranche.entry_price for tranche in position.tranches} == {expected}
