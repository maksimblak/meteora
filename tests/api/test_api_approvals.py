"""APPROVAL preview and one-shot approval (G02, G17, D03, D08) through ``ExecutionService``."""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import pytest

from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.domain.screening.models import DecisionFlag
from src.execution.contracts import DlmmOperation, ExecutionPhase
from src.modes import DailyRiskService, KillSwitchPolicy, ModeService
from src.persistence.factory import Repositories
from src.worker.policy import PipelinePolicy
from tests.api.support import (
    NEW_POSITION,
    OPERATOR_TOKEN,
    ApprovalCase,
    Panel,
    Scenario,
    awaiting_approval,
    awaiting_exit,
    factory_repositories,
    flag_decision,
    harness_over,
    make_panel,
    populated,
    promote_to_approval,
    requote,
)
from tests.worker.harness import NOW, harness_policy

ALL_MARKS = {
    "twitter_hype": True,
    "developer_supply": True,
    "no_pvp_copies": True,
    "bubblemaps_clusters": True,
}


@pytest.fixture
def approval(tmp_path: Path) -> Iterator[tuple[Panel, ApprovalCase]]:
    from src.core.clock import FrozenClock

    repositories = factory_repositories(tmp_path)
    harness = harness_over(repositories, FrozenClock(NOW))
    scenario = populated(harness)
    flag_decision(harness, scenario.accepted.decision_id, DecisionFlag.TOTAL_FEES_UNAVAILABLE)
    promote_to_approval(harness)
    case = awaiting_approval(scenario, repositories)
    panel = make_panel(
        tmp_path, repositories=repositories, harness=harness, approvals=case.setup
    )
    yield panel, case
    repositories.close()


def approve_body(preview: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body = {
        "approval_id": str(uuid4()),
        "command_digest": preview["command_digest"],
        "simulation_digest": preview["simulation_digest"],
        "checklist": ALL_MARKS,
        "comment": "проверил вручную",
        "operator_token": OPERATOR_TOKEN,
        "confirm_irreversible": True,
    }
    body.update(overrides)
    return body


def phase(panel: Panel, case: ApprovalCase) -> ExecutionPhase:
    record = asyncio.run(panel.repositories.execution.get(case.command.command_id))
    assert record is not None
    return record.phase


def test_preview_shows_irreversibility_simulation_expiry_costs_plan_diff_and_checklist(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval

    preview = panel.get(f"/api/v1/approvals/{case.command.command_id}").json()

    assert preview["irreversible"] is True and "нельзя отменить" in preview["irreversibility_note"]
    assert preview["phase"] == "AWAITING_APPROVAL" and preview["can_approve"] is True
    simulation = {item["name"]: item["value"] for item in preview["simulation"]}
    assert simulation["price_impact_bps"] == "5" and simulation["success"] == "True"
    assert preview["costs"] == {
        "network_fee_lamports": "5000",
        "priority_fee_lamports": "1000",
        "compute_units": 120000,
        "price_impact_bps": 5,
        "max_slippage_bps": 100,
        "estimated_loss_base_units": panel.harness.decisions.plans[
            case.plan_id
        ].sized_plan.estimated_loss_base_units,
    }
    # The shortest of: operator TTL (90 s), plan expiry, simulation TTL (2 min).
    assert preview["approval_expires_at"] == (NOW + timedelta(seconds=90)).isoformat().replace(
        "+00:00", "Z"
    )
    assert preview["seconds_left"] == 90
    diff = {row["field"]: row for row in preview["plan_diff"]}
    assert diff["strategy"]["matches"] is True
    assert diff["bin_range"]["command"] == diff["bin_range"]["simulated"]
    assert diff["bin_range"]["matches"] is True
    assert diff["price_impact_bps"]["matches"] is True
    assert diff["expires_at"]["matches"] is True
    # D06: the executor creates the entry position; the preview names the simulated one
    # and compares the anchor the command was built on with the chain's active bin.
    assert preview["position_address"] == NEW_POSITION
    assert diff["active_bin_id"]["simulated"] == "8000"
    assert diff["active_bin_id"]["matches"] is True
    assert [item["label"] for item in preview["checklist"]] == [
        "Twitter и хайп проверены",
        "Запас монет у девелопера проверен",
        "Нет гонки копий монеты (PvP)",
        "Кластеры на Bubblemaps проверены",
    ]
    assert preview["decision_id"] == str(case.decision_id)
    assert [item["message"] for item in preview["notices"]] == [
        "total fees: нет данных — проверь в GMGN"
    ]


def test_approval_without_every_checklist_mark_is_refused(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/api/v1/approvals/{case.command.command_id}"
    preview = panel.get(path).json()

    response = panel.bearer(
        path, approve_body(preview, checklist={**ALL_MARKS, "bubblemaps_clusters": False})
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CHECKLIST_INCOMPLETE"
    assert response.json()["error"]["details"]["missing"] == ["bubblemaps_clusters"]
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL
    assert not [
        event
        for event in panel.harness.audit.events
        if event.event_type is EventType.APPROVAL_REQUESTED
    ]


def test_approval_requires_reauthentication_and_the_irreversibility_confirmation(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/api/v1/approvals/{case.command.command_id}"
    preview = panel.get(path).json()

    unconfirmed = panel.bearer(path, approve_body(preview, confirm_irreversible=False))
    mistyped = panel.bearer(path, approve_body(preview, operator_token="typo"))  # noqa: S106

    assert unconfirmed.status_code == 403
    assert mistyped.status_code == 403
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL


def test_approval_is_audited_with_marks_and_time_then_one_shot(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/api/v1/approvals/{case.command.command_id}"
    preview = panel.get(path).json()
    body = approve_body(preview)

    approved = panel.bearer(path, body)
    repeated = panel.bearer(path, body)
    second = panel.bearer(path, approve_body(preview))

    assert approved.status_code == 200, approved.text
    assert approved.json()["phase"] == "APPROVED"
    assert approved.json()["approval_id"] == body["approval_id"]
    assert repeated.status_code == 200 and repeated.json() == approved.json()
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "APPROVAL_NOT_PENDING"
    assert second.json()["error"]["details"]["phase"] == "APPROVED"
    assert phase(panel, case) is ExecutionPhase.APPROVED
    events = [
        event
        for event in panel.harness.audit.events
        if event.event_type is EventType.APPROVAL_REQUESTED
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["stage"] == "approved"
    assert payload["checklist"] == ALL_MARKS
    assert payload["checklist_confirmed_at"] == NOW.isoformat()
    assert payload["comment"] == "проверил вручную"
    assert payload["approval_id"] == body["approval_id"]
    assert OPERATOR_TOKEN not in str(payload)
    refreshed = panel.get(path).json()
    assert refreshed["phase"] == "APPROVED" and refreshed["can_approve"] is False


def test_a_preview_whose_quote_changed_or_expired_cannot_be_approved(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/api/v1/approvals/{case.command.command_id}"
    preview = panel.get(path).json()

    stale = panel.bearer(path, approve_body(preview, simulation_digest="f" * 64))
    panel.harness.clock.advance(timedelta(minutes=3))
    expired = panel.bearer(path, approve_body(preview))
    late_preview = panel.get(path).json()

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "APPROVAL_PREVIEW_STALE"
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "APPROVAL_BLOCKED"
    assert "SIMULATION_STALE" in expired.json()["error"]["details"]["blockers"]
    assert late_preview["can_approve"] is False and late_preview["seconds_left"] == 0
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL


def test_a_kill_switch_blocks_approval_and_the_preview_says_why(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/api/v1/approvals/{case.command.command_id}"
    panel.bearer("/api/v1/kill-switch/activate", {"reason": "стоп"})

    preview = panel.get(path).json()
    response = panel.bearer(path, approve_body(preview))

    codes = [item["code"] for item in preview["blockers"]]
    assert "KILL_SWITCH_ACTIVE" in codes and preview["can_approve"] is False
    assert response.status_code == 409


def test_an_unwired_deployment_explains_why_nothing_can_be_approved(tmp_path: Path) -> None:
    panel = make_panel(tmp_path)
    try:
        queue = panel.get("/api/v1/approvals").json()
        missing = panel.get(f"/api/v1/approvals/{uuid4()}")
    finally:
        panel.repositories.close()

    codes = [item["code"] for item in queue["blockers"]]
    assert queue["pending"] == []
    assert {"TRADING_MODE_SHADOW", "MODE_NOT_APPROVAL", "ENTRY_ANCHOR_UNAVAILABLE"} <= set(codes)
    assert missing.status_code == 404


# --- D08: an exit swaps nothing, so its quote never keeps a position open --------------

Build = Callable[[Scenario, Repositories], ApprovalCase]


class OpenDesk(Protocol):
    def __call__(
        self, build: Build, *, policy: PipelinePolicy | None = None
    ) -> tuple[Panel, ApprovalCase]: ...


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[OpenDesk]:
    """An APPROVAL deployment whose pending command is the one ``build`` prepares; the
    panel and the execution service share ``policy`` (its kill-switch policy)."""

    opened: list[Repositories] = []

    def open_desk(
        build: Build, *, policy: PipelinePolicy | None = None
    ) -> tuple[Panel, ApprovalCase]:
        repositories = factory_repositories(tmp_path)
        opened.append(repositories)
        harness = harness_over(repositories, FrozenClock(NOW), policy=policy)
        scenario = populated(harness)
        promote_to_approval(harness)
        case = build(scenario, repositories)
        panel = make_panel(
            tmp_path, repositories=repositories, harness=harness, approvals=case.setup
        )
        return panel, case

    yield open_desk
    for repositories in opened:
        repositories.close()


def exit_of(operation: DlmmOperation, impact: int | None) -> Build:
    def build(scenario: Scenario, repositories: Repositories) -> ApprovalCase:
        return awaiting_exit(
            scenario, repositories, operation=operation, price_impact_bps=impact
        )

    return build


def entry_quoted(impact: int | None) -> Build:
    def build(scenario: Scenario, repositories: Repositories) -> ApprovalCase:
        case = awaiting_approval(scenario, repositories)
        requote(repositories, case, impact)
        return case

    return build


def approval_events(panel: Panel) -> list[Any]:
    return [
        event
        for event in panel.harness.audit.events
        if event.event_type is EventType.APPROVAL_REQUESTED
    ]


# The commands are built with max_slippage_bps=100: 9000 bps is far above that limit.
@pytest.mark.parametrize(
    ("operation", "impact"),
    [
        pytest.param(DlmmOperation.CLOSE, 9_000, id="close-above-limit"),
        pytest.param(DlmmOperation.CLOSE, None, id="close-unmeasured"),
        pytest.param(DlmmOperation.REMOVE, 9_000, id="remove-above-limit"),
        pytest.param(DlmmOperation.REMOVE, None, id="remove-unmeasured"),
    ],
)
def test_an_exit_is_approved_whatever_its_quote_shows(
    desk: OpenDesk,
    operation: DlmmOperation,
    impact: int | None,
) -> None:
    panel, case = desk(exit_of(operation, impact))
    path = f"/api/v1/approvals/{case.command.command_id}"

    preview = panel.get(path).json()
    approved = panel.bearer(path, approve_body(preview))

    assert preview["operation"] == operation.value
    assert preview["blockers"] == [] and preview["can_approve"] is True
    # Shown to the operator, not enforced: neither a blocker nor a plan discrepancy.
    assert preview["costs"]["price_impact_bps"] == impact
    diff = {row["field"]: row for row in preview["plan_diff"]}
    assert diff["price_impact_bps"]["simulated"] == (None if impact is None else str(impact))
    assert diff["price_impact_bps"]["matches"] is None
    assert approved.status_code == 200, approved.text
    assert approved.json()["phase"] == "APPROVED"
    assert phase(panel, case) is ExecutionPhase.APPROVED
    (event,) = approval_events(panel)
    assert event.payload["stage"] == "approved"
    assert event.payload["price_impact_bps"] == impact


@pytest.mark.parametrize(
    ("impact", "blocker"),
    [
        pytest.param(9_000, "PRICE_IMPACT_ABOVE_LIMIT", id="above-limit"),
        pytest.param(None, "PRICE_IMPACT_UNMEASURED", id="unmeasured"),
    ],
)
def test_an_entry_over_the_impact_limit_or_unmeasured_stays_blocked(
    desk: OpenDesk,
    impact: int | None,
    blocker: str,
) -> None:
    panel, case = desk(entry_quoted(impact))
    path = f"/api/v1/approvals/{case.command.command_id}"

    preview = panel.get(path).json()
    response = panel.bearer(path, approve_body(preview))

    assert preview["operation"] == "OPEN"
    assert [item["code"] for item in preview["blockers"]] == [blocker]
    assert preview["can_approve"] is False
    diff = {row["field"]: row for row in preview["plan_diff"]}
    assert diff["price_impact_bps"]["matches"] is False
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_BLOCKED"
    assert response.json()["error"]["details"]["blockers"] == [blocker]
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL
    assert approval_events(panel) == []


def test_an_exit_over_the_impact_limit_keeps_every_other_guard(
    desk: OpenDesk,
) -> None:
    panel, case = desk(exit_of(DlmmOperation.CLOSE, 9_000))
    path = f"/api/v1/approvals/{case.command.command_id}"
    preview = panel.get(path).json()

    unchecked = panel.bearer(
        path, approve_body(preview, checklist={**ALL_MARKS, "no_pvp_copies": False})
    )
    mistyped = panel.bearer(path, approve_body(preview, operator_token="typo"))  # noqa: S106
    unconfirmed = panel.bearer(path, approve_body(preview, confirm_irreversible=False))
    requoted = panel.bearer(path, approve_body(preview, simulation_digest="f" * 64))
    panel.bearer("/api/v1/kill-switch/activate", {"reason": "стоп"})  # HOLD_AND_ALERT
    killed = panel.bearer(path, approve_body(preview))
    panel.harness.clock.advance(timedelta(minutes=3))
    stale = panel.bearer(path, approve_body(preview))
    panel.login()
    forged = panel.post(path, json=approve_body(preview))  # session cookie, no CSRF token

    assert unchecked.status_code == 422
    assert unchecked.json()["error"]["code"] == "CHECKLIST_INCOMPLETE"
    assert mistyped.status_code == 403 and unconfirmed.status_code == 403
    assert requoted.status_code == 409
    assert requoted.json()["error"]["code"] == "APPROVAL_PREVIEW_STALE"
    assert killed.status_code == 409
    assert "KILL_SWITCH_ACTIVE" in killed.json()["error"]["details"]["blockers"]
    assert stale.status_code == 409
    assert "SIMULATION_STALE" in stale.json()["error"]["details"]["blockers"]
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "CSRF_FAILED"
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL
    assert approval_events(panel) == []


# --- emergency stops (spec 7.3): an APPROVAL position may still be left ----------------

# The configured pipeline policy decides, as for the worker and ExecutionService.
ORDERLY_EXIT = replace(harness_policy(), kill_switch_policy=KillSwitchPolicy.ORDERLY_EXIT)
HOLD_AND_ALERT = harness_policy()


def kill_switch(panel: Panel) -> None:
    policy = panel.harness.policy.kill_switch_policy
    response = panel.bearer(
        "/api/v1/kill-switch/activate", {"reason": "авария", "policy": policy.value}
    )
    assert response.status_code == 200, response.text


def kill_switch_then_wait(panel: Panel) -> None:
    kill_switch(panel)
    panel.harness.clock.advance(timedelta(minutes=3))  # past the 2-minute simulation TTL


def daily_stop(panel: Panel) -> None:
    harness = panel.harness
    # A 5% realized loss of the 1,000,000 start equity: past the 4% daily stop.
    day = DailyRiskService(harness.modes, harness.clock).record(
        Decimal("-50000"), Decimal(0), harness.policy.ruleset_version
    )
    assert day.daily_stop_triggered is True


def manual_shadow(panel: Panel) -> None:
    harness = panel.harness
    ModeService(harness.modes, harness.clock, harness.policy.ruleset_version).transition(
        TradingMode.SHADOW, None, "operator-test-confirmation"
    )


def blocker_codes(preview: dict[str, Any]) -> list[str]:
    return [item["code"] for item in preview["blockers"]]


@pytest.mark.parametrize(
    ("operation", "policy", "stop"),
    [
        pytest.param(DlmmOperation.CLOSE, ORDERLY_EXIT, kill_switch, id="close-orderly-kill"),
        pytest.param(DlmmOperation.REMOVE, ORDERLY_EXIT, kill_switch, id="remove-orderly-kill"),
        pytest.param(DlmmOperation.CLOSE, HOLD_AND_ALERT, daily_stop, id="close-daily-stop"),
        pytest.param(DlmmOperation.REMOVE, ORDERLY_EXIT, daily_stop, id="remove-daily-stop"),
    ],
)
def test_an_exit_of_an_approval_position_is_approved_after_an_emergency_stop(
    desk: OpenDesk,
    operation: DlmmOperation,
    policy: PipelinePolicy,
    stop: Callable[[Panel], None],
) -> None:
    panel, case = desk(exit_of(operation, 5), policy=policy)
    path = f"/api/v1/approvals/{case.command.command_id}"
    stop(panel)

    preview = panel.get(path).json()
    requoted = panel.bearer(path, approve_body(preview, simulation_digest="f" * 64))
    unchecked = panel.bearer(
        path, approve_body(preview, checklist={**ALL_MARKS, "twitter_hype": False})
    )
    approved = panel.bearer(path, approve_body(preview))

    state = panel.repositories.mode.mode_state()
    assert state.mode is TradingMode.SHADOW  # the stop demoted the deployment
    assert blocker_codes(preview) == [] and preview["can_approve"] is True
    assert requoted.status_code == 409
    assert requoted.json()["error"]["code"] == "APPROVAL_PREVIEW_STALE"
    assert unchecked.status_code == 422
    assert approved.status_code == 200, approved.text
    assert approved.json()["phase"] == "APPROVED"
    assert phase(panel, case) is ExecutionPhase.APPROVED


@pytest.mark.parametrize(
    ("build", "policy", "stop", "expected"),
    [
        pytest.param(
            exit_of(DlmmOperation.CLOSE, 5),
            HOLD_AND_ALERT,
            kill_switch,
            {"KILL_SWITCH_ACTIVE", "MODE_NOT_APPROVAL"},
            id="exit-held-by-the-kill-switch",
        ),
        pytest.param(
            exit_of(DlmmOperation.CLOSE, 5),
            ORDERLY_EXIT,
            kill_switch_then_wait,
            {"SIMULATION_STALE"},
            id="orderly-exit-with-a-stale-simulation",
        ),
        pytest.param(
            exit_of(DlmmOperation.CLOSE, 5),
            ORDERLY_EXIT,
            manual_shadow,
            {"MODE_NOT_APPROVAL"},
            id="exit-in-shadow-without-a-stop",
        ),
        pytest.param(
            awaiting_approval,
            ORDERLY_EXIT,
            kill_switch,
            {"KILL_SWITCH_ACTIVE", "MODE_NOT_APPROVAL"},
            id="entry-under-an-orderly-kill-switch",
        ),
        pytest.param(
            awaiting_approval,
            ORDERLY_EXIT,
            daily_stop,
            {"DAILY_STOP_ACTIVE", "MODE_NOT_APPROVAL"},
            id="entry-after-the-daily-stop",
        ),
        pytest.param(
            awaiting_approval,
            HOLD_AND_ALERT,
            manual_shadow,
            {"MODE_NOT_APPROVAL"},
            id="entry-in-shadow",
        ),
    ],
)
def test_emergency_stops_still_block_entries_held_exits_and_stale_quotes(
    desk: OpenDesk,
    build: Build,
    policy: PipelinePolicy,
    stop: Callable[[Panel], None],
    expected: set[str],
) -> None:
    panel, case = desk(build, policy=policy)
    path = f"/api/v1/approvals/{case.command.command_id}"
    stop(panel)

    preview = panel.get(path).json()
    response = panel.bearer(path, approve_body(preview))

    assert set(blocker_codes(preview)) == expected and preview["can_approve"] is False
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_BLOCKED"
    assert set(response.json()["error"]["details"]["blockers"]) == expected
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL
    assert approval_events(panel) == []
