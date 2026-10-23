"""Position cycle: marks, exits, kill-switch hold policy and the 4% daily stop."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from src.domain.models.events import EventType
from src.domain.strategy import PositionActionKind
from src.modes import DailyRiskService, KillSwitchPolicy, ModeService, PendingPlan
from tests.worker.harness import NOW, POOL, Harness, snapshot, spike_history


def open_position(harness: Harness) -> str:
    harness.ingest(spike_history())

    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    assert outcome.entry is not None and outcome.entry.position_id is not None
    return str(outcome.entry.position_id)


def next_minute(harness: Harness, **market: str) -> None:
    harness.clock.advance(timedelta(minutes=1))
    harness.ingest([snapshot(harness.clock.now(), **market)])


def test_unchanged_market_holds_the_position() -> None:
    harness = Harness()
    position_id = open_position(harness)
    next_minute(harness, short="400", long="1200")

    report = asyncio.run(harness.positions.run_all())

    assert [(review.position_id, review.action) for review in report.reviews] == [
        (position_id, PositionActionKind.HOLD)
    ]
    assert harness.modes.open_positions() == (position_id,)
    assert report.risk_day is not None and report.risk_day.daily_stop_triggered is False


def test_volume_decay_exit_is_audited_then_closes_the_shadow_position() -> None:
    harness = Harness()
    position_id = open_position(harness)
    next_minute(harness, short="50", long="1200")

    report = asyncio.run(harness.positions.run_all())

    (review,) = report.reviews
    assert review.position_id == position_id
    assert review.action is PositionActionKind.EXIT and "VOLUME_DECAY" in review.reasons
    assert review.closed is True
    assert harness.modes.open_positions() == ()
    assert len(harness.accounting.outcomes()) == 1
    assert harness.event_types()[-1] == EventType.POSITION_ACTION_PROPOSED.value
    assert report.risk_day is not None and report.risk_day.daily_stop_triggered is False


def test_crash_marks_trip_the_daily_stop_which_blocks_entries_and_alerts() -> None:
    harness = Harness()
    position_id = open_position(harness)
    next_minute(harness, short="400", long="1200", price="0.5")

    report = asyncio.run(harness.positions.run_all())

    assert report.risk_day is not None and report.risk_day.daily_stop_triggered is True
    assert report.conservative_unrealized_pnl + report.realized_pnl <= -40_000
    assert harness.modes.open_positions() == ()
    assert harness.accounting.position_for(position_id).status.value == "CLOSED"
    assert DailyRiskService(harness.modes, harness.clock).entries_allowed() is False
    assert [alert.code for alert in harness.modes.alerts() if alert.open] == [
        "DAILY_STOP_TRIGGERED"
    ]


def test_a_position_cap_breach_activates_the_kill_switch() -> None:
    harness = Harness()
    harness.keeper.ensure_open()
    for index in range(4):
        plan = PendingPlan(
            plan_id=uuid4(),
            correlation_id=f"breach-{index}",
            admitted_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        assert harness.modes.admit_entry(plan, 10) == ()
        harness.modes.claim_entry(plan.plan_id, NOW)
        harness.modes.confirm_entry(plan.plan_id, f"orphan-{index}")

    asyncio.run(harness.positions.run_all())

    state = harness.modes.mode_state()
    assert state.kill_switch_active is True
    kill = [alert for alert in harness.modes.alerts() if alert.code == "KILL_SWITCH_ACTIVE"]
    assert len(kill) == 1 and "RISK_INVARIANT_BREACH" in kill[0].message
    assert all(not action.transaction_submitted for action in harness.modes.position_actions())


def test_kill_switch_hold_policy_keeps_positions_open_and_never_trades() -> None:
    harness = Harness()
    position_id = open_position(harness)
    ModeService(harness.modes, harness.clock, "rules-1").activate_kill_switch(
        "operator stop", KillSwitchPolicy.HOLD_AND_ALERT
    )
    next_minute(harness, short="400", long="1200", price="0.8")
    receipts_before = len(harness.accounting.ledger_entries())

    report = asyncio.run(harness.positions.run_all())

    (review,) = report.reviews
    assert review.action is PositionActionKind.EXIT and review.closed is False
    assert "KILL_SWITCH_ACTIVE" in review.reasons
    assert harness.modes.open_positions() == (position_id,)
    assert len(harness.accounting.ledger_entries()) == receipts_before
