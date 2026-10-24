"""F4: the take profit never closes a real (APPROVAL/LIVE) position on the SHADOW model.

The worker cannot measure what a gateway position is actually worth (the executor has no
valuation of a position), so for a non-SHADOW position the take profit does not fire and
says so in the audited review (spec 6.8, T19 review). A SHADOW position keeps it
(``tests/worker/test_t19_positions.py``).
"""

from datetime import timedelta

from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmOperation
from tests.domain.guide_fixtures import guide_history, guide_snapshot
from tests.worker.test_gateway_execution import ANCHOR, StaticPoolStates, approve, ingest, run_job
from tests.worker.test_t19_entries import approval_runtime, enter


def test_a_real_position_is_not_taken_profit_on_the_shadow_model_and_the_review_says_so() -> None:
    runtime, executor, repos = approval_runtime(
        guide_history(), StaticPoolStates(ANCHOR.active_bin_id, bin_step=250)
    )
    outcome = enter(runtime)
    assert outcome.entry is not None and outcome.entry.reason == "AWAITING_APPROVAL"
    ((command, _),) = executor.simulated
    approve(runtime, command.command_id, timedelta(seconds=60))
    run_job(runtime, "candidate")
    (position_id,) = repos.mode.open_positions()
    position = repos.accounting.position_for(position_id)
    assert position is not None and position.mode is TradingMode.APPROVAL
    # A minute later the pool earned 100 000 000 / 100 of fees in its 5m window: a minute of
    # it on a 150 000 TVL is 133% of the position by the SHADOW model - still far above the
    # 5% target after the entry's and the exit's fixed 5 000-lamport fees on 13 240 invested.
    runtime.clock.advance(timedelta(minutes=1))
    ingest(repos, guide_snapshot(runtime.clock.now(), short="100000000"))

    run_job(runtime, "position")

    proposed = [
        event.payload
        for event in repos.audit.events
        if event.event_type is EventType.POSITION_ACTION_PROPOSED
    ]
    assert [payload["action"] for payload in proposed] == ["HOLD"]
    assert "TAKE_PROFIT" not in proposed[0]["reasons"]
    assert proposed[0]["take_profit"] == "UNMEASURED"
    assert repos.mode.open_positions() == (position_id,)
    assert [item.operation for item, _ in executor.simulated] == [DlmmOperation.OPEN]
