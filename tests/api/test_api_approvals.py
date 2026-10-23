"""APPROVAL preview and one-shot approval (G02, G17, D03) through ``ExecutionService``."""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.domain.models.events import EventType
from src.domain.screening.models import DecisionFlag
from src.execution.contracts import ExecutionPhase
from tests.api.support import (
    NEW_POSITION,
    OPERATOR_TOKEN,
    ApprovalCase,
    Panel,
    awaiting_approval,
    factory_repositories,
    flag_decision,
    harness_over,
    make_panel,
    populated,
    promote_to_approval,
)
from tests.worker.harness import NOW

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
