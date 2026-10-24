"""F3: closing critical alerts from the panel (G13.2, spec 7.1) and the F2 tails of the panel
(kill-switch policy from the mode state, expired requests are not "waiting")."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.modes.models import CriticalAlert
from tests.api.support import (
    OPERATOR_TOKEN,
    POOL_B,
    POOL_C,
    Panel,
    awaiting_approval,
    factory_repositories,
    harness_over,
    ingest_universe,
    make_panel,
    other_pool,
    populated,
    promote_to_approval,
    record_observations,
)
from tests.worker.harness import NOW


def resolve_path(alert_id: UUID) -> str:
    return f"/api/v1/alerts/{alert_id}/resolve"


def raise_alert(panel: Panel, code: str = "DAILY_STOP_TRIGGERED") -> UUID:
    alert = CriticalAlert(
        alert_id=uuid4(), code=code, message="daily loss limit reached", created_at=NOW
    )
    panel.repositories.mode.raise_alert(alert)
    return alert.alert_id


def error_code(response: object) -> str:
    return str(response.json()["error"]["code"])  # type: ignore[attr-defined]


def open_alert_ids(panel: Panel) -> set[str]:
    return {item["alert_id"] for item in panel.get("/api/v1/system/status").json()["alerts"]}


GOOD = {
    "reason": "убыток разобран, стоп осознан",
    "operator_token": OPERATOR_TOKEN,
    "confirm": True,
}


def test_a_session_without_csrf_cannot_close_an_alert(panel: Panel) -> None:
    alert_id = raise_alert(panel)
    panel.login()

    response = panel.post(resolve_path(alert_id), json=GOOD)

    assert response.status_code == 403 and error_code(response) == "CSRF_FAILED"
    assert str(alert_id) in open_alert_ids(panel)


def test_closing_an_alert_needs_the_token_typed_again_and_a_confirmation(panel: Panel) -> None:
    alert_id = raise_alert(panel)

    no_token = panel.bearer(resolve_path(alert_id), {"reason": "ок", "confirm": True})
    unconfirmed = panel.bearer(
        resolve_path(alert_id), {"reason": "ок", "operator_token": OPERATOR_TOKEN}
    )
    no_reason = panel.bearer(
        resolve_path(alert_id), {"reason": " ", "operator_token": OPERATOR_TOKEN, "confirm": True}
    )

    assert no_token.status_code == 403 and error_code(no_token) == "REAUTHENTICATION_FAILED"
    assert unconfirmed.status_code == 403 and error_code(unconfirmed) == "CONFIRMATION_REQUIRED"
    assert no_reason.status_code == 422
    assert str(alert_id) in open_alert_ids(panel)


def test_a_closed_alert_is_audited_and_no_longer_blocks_the_shadow_evidence(panel: Panel) -> None:
    alert_id = raise_alert(panel)
    record_observations(panel.harness, 7)
    blocked = panel.bearer("/api/v1/promotion/evidence").json()

    closed = panel.bearer(resolve_path(alert_id), GOOD)

    assert "CRITICAL_ALERTS_OPEN" in [item["code"] for item in blocked["reasons"]]
    assert closed.status_code == 200
    body = closed.json()
    assert body["alert_id"] == str(alert_id) and body["open"] is False
    assert str(alert_id) not in open_alert_ids(panel)
    audit = [
        event
        for event in panel.harness.audit.events
        if event.aggregate_id == alert_id and event.event_type is EventType.CRITICAL_ALERT_RAISED
    ]
    assert [event.payload["stage"] for event in audit] == ["resolved"]
    assert audit[0].payload["code"] == "DAILY_STOP_TRIGGERED"
    assert audit[0].payload["reason"] == GOOD["reason"]
    assert audit[0].payload["operator"].startswith("operator:bearer")
    assert OPERATOR_TOKEN not in str(audit[0].payload)
    evidence = panel.bearer("/api/v1/promotion/evidence").json()
    assert "CRITICAL_ALERTS_OPEN" not in [item["code"] for item in evidence["reasons"]]
    assert evidence["open_critical_alerts"] == 0


def test_unknown_resolved_and_active_kill_switch_alerts_are_refused(panel: Panel) -> None:
    alert_id = raise_alert(panel)
    assert panel.bearer(resolve_path(alert_id), GOOD).status_code == 200

    again = panel.bearer(resolve_path(alert_id), GOOD)
    unknown = panel.bearer(resolve_path(uuid4()), GOOD)
    panel.bearer("/api/v1/kill-switch/activate", {"reason": "стоп"})
    (kill,) = [
        item for item in panel.get("/api/v1/system/status").json()["alerts"]
        if item["code"] == "KILL_SWITCH_ACTIVE"
    ]
    active = panel.bearer(resolve_path(UUID(kill["alert_id"])), GOOD)

    assert again.status_code == 409 and error_code(again) == "ALERT_RESOLVE_REJECTED"
    assert unknown.status_code == 404 and error_code(unknown) == "ALERT_NOT_FOUND"
    assert active.status_code == 409 and error_code(active) == "ALERT_RESOLVE_REJECTED"
    assert kill["alert_id"] in open_alert_ids(panel)


def test_the_effective_config_shows_the_kill_switch_policy_of_the_mode_state(panel: Panel) -> None:
    before = panel.get("/api/v1/config/effective").json()["kill_switch"]
    panel.bearer("/api/v1/kill-switch/activate", {"reason": "стоп", "policy": "ORDERLY_EXIT"})
    after = panel.get("/api/v1/config/effective").json()

    # The deployment's rules default to HOLD_AND_ALERT; the operator chose ORDERLY_EXIT.
    assert panel.harness.policy.kill_switch_policy.value == "HOLD_AND_ALERT"
    assert before == {"active": False, "policy": None, "label": before["label"]}
    assert after["kill_switch"]["active"] is True
    assert after["kill_switch"]["policy"] == "ORDERLY_EXIT"
    assert "выход" in after["kill_switch"]["label"]
    assert "kill_switch_policy" not in after["thresholds"]


def test_the_dashboard_counts_only_outcomes_of_the_current_cycles(panel: Panel) -> None:
    harness = panel.harness
    ingest_universe(harness, [other_pool(POOL_B), other_pool(POOL_C, tvl="999")])

    async def run() -> None:
        harness.keeper.ensure_open()
        for address in (POOL_B.address.root, POOL_C.address.root):
            await harness.pipeline.evaluate(address, NOW)
        # POOL_B is skipped by the next two cycles (as a pool with an entry in work is).
        for later in (NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)):
            await harness.pipeline.evaluate(POOL_C.address.root, later)

    asyncio.run(run())

    counts = panel.get("/api/v1/system/status").json()["universe"]["decision_counts"]

    assert counts == {"REJECT": 1}


def test_an_expired_request_is_not_counted_as_waiting_for_the_operator(tmp_path: Path) -> None:
    repositories = factory_repositories(tmp_path)
    harness = harness_over(repositories, FrozenClock(NOW))
    scenario = populated(harness)
    promote_to_approval(harness)
    case = awaiting_approval(scenario, repositories)
    panel = make_panel(tmp_path, repositories=repositories, harness=harness, approvals=case.setup)
    try:
        waiting = panel.get("/api/v1/system/status").json()["approvals_pending"]
        harness.clock.advance(case.command.expires_at - NOW + timedelta(seconds=1))
        expired = panel.get("/api/v1/system/status").json()["approvals_pending"]
        stream = panel.get("/api/v1/stream/status").text
    finally:
        repositories.close()

    assert waiting == 1
    assert expired == 0
    assert '"approvals_pending":0' in stream
