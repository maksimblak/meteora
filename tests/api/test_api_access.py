"""Operator access (spec 5.6, G03): writes need auth + CSRF; money actions need re-auth.

Downgrade and kill-switch activation apply immediately for an authenticated operator;
promotion and kill-switch release refuse without the token typed again and a
confirmation flag.
"""

from pathlib import Path

from tests.api.support import (
    OPERATOR_TOKEN,
    Panel,
    make_panel,
    promote_to_approval,
    record_observations,
)

KILL = "/api/v1/kill-switch/activate"
RELEASE = "/api/v1/kill-switch/release"
TRANSITIONS = "/api/v1/mode/transitions"


def error_code(response: object) -> str:
    body = response.json()  # type: ignore[attr-defined]
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    return str(body["error"]["code"])


def mode_state(panel: Panel) -> dict[str, object]:
    return dict(panel.get("/api/v1/mode").json()["mode"])


def test_state_changing_endpoints_refuse_anonymous_and_wrong_token_callers(panel: Panel) -> None:
    anonymous = panel.post(KILL, json={"reason": "stop"})
    wrong = panel.post(
        KILL, json={"reason": "stop"}, headers={"Authorization": "Bearer not-the-token"}
    )

    assert anonymous.status_code == 401 and error_code(anonymous) == "UNAUTHORIZED"
    assert wrong.status_code == 401 and error_code(wrong) == "UNAUTHORIZED"
    assert mode_state(panel)["kill_switch_active"] is False


def test_a_web_session_without_its_csrf_token_cannot_change_state(panel: Panel) -> None:
    csrf = panel.login()

    forged = panel.post(KILL, json={"reason": "stop"})
    wrong = panel.post(KILL, json={"reason": "stop"}, headers={"X-CSRF-Token": "0" * 64})
    allowed = panel.post(KILL, json={"reason": "stop"}, headers={"X-CSRF-Token": csrf})

    assert forged.status_code == 403 and error_code(forged) == "CSRF_FAILED"
    assert wrong.status_code == 403 and error_code(wrong) == "CSRF_FAILED"
    assert allowed.status_code == 200
    assert mode_state(panel)["kill_switch_active"] is True


def test_without_an_operator_token_every_action_is_closed(tmp_path: Path) -> None:
    from tests.api.support import operator_settings

    closed = make_panel(tmp_path, settings=operator_settings(operator_token=None))
    try:
        response = closed.bearer(KILL, {"reason": "stop"})
        login = closed.post("/login", data={"token": OPERATOR_TOKEN})
    finally:
        closed.repositories.close()

    assert response.status_code == 503 and error_code(response) == "OPERATOR_AUTH_NOT_CONFIGURED"
    assert login.status_code == 503


def test_kill_switch_activates_at_once_but_release_needs_reauthentication(panel: Panel) -> None:
    activated = panel.bearer(KILL, {"reason": "аварийная остановка", "policy": "HOLD_AND_ALERT"})
    unconfirmed = panel.bearer(RELEASE, {"reason": "ok", "operator_token": OPERATOR_TOKEN})
    mistyped = panel.bearer(RELEASE, {"reason": "ok", "operator_token": "typo", "confirm": True})

    assert activated.status_code == 200
    assert activated.json()["mode"]["kill_switch_active"] is True
    assert unconfirmed.status_code == 403 and error_code(unconfirmed) == "CONFIRMATION_REQUIRED"
    assert mistyped.status_code == 403 and error_code(mistyped) == "REAUTHENTICATION_FAILED"
    assert mode_state(panel)["kill_switch_active"] is True

    released = panel.bearer(
        RELEASE, {"reason": "проверено", "operator_token": OPERATOR_TOKEN, "confirm": True}
    )

    assert released.status_code == 200 and released.json()["kill_switch_active"] is False


def test_promotion_needs_reauthentication_and_evidence_while_downgrade_is_immediate(
    panel: Panel,
) -> None:
    no_reauth = panel.bearer(TRANSITIONS, {"target_mode": "APPROVAL", "reason": "готово"})
    no_evidence = panel.bearer(
        TRANSITIONS,
        {
            "target_mode": "APPROVAL",
            "reason": "готово",
            "operator_token": OPERATOR_TOKEN,
            "confirm": True,
        },
    )
    thin = panel.bearer("/api/v1/promotion/evidence")
    rejected = panel.bearer(
        TRANSITIONS,
        {
            "target_mode": "APPROVAL",
            "reason": "готово",
            "evidence_id": thin.json()["evidence_id"],
            "operator_token": OPERATOR_TOKEN,
            "confirm": True,
        },
    )

    assert no_reauth.status_code == 403 and error_code(no_reauth) == "CONFIRMATION_REQUIRED"
    assert no_evidence.status_code == 422 and error_code(no_evidence) == "EVIDENCE_REQUIRED"
    assert thin.status_code == 200 and thin.json()["allowed"] is False
    assert thin.json()["minimum_days_remaining"] == 7
    assert thin.json()["recommended_days_remaining"] == 14
    assert rejected.status_code == 409 and error_code(rejected) == "MODE_TRANSITION_REJECTED"
    assert mode_state(panel)["mode"] == "SHADOW"

    record_observations(panel.harness, 7)
    evidence = panel.bearer("/api/v1/promotion/evidence").json()
    promoted = panel.bearer(
        TRANSITIONS,
        {
            "target_mode": "APPROVAL",
            "reason": "7 дней shadow пройдены",
            "evidence_id": evidence["evidence_id"],
            "operator_token": OPERATOR_TOKEN,
            "confirm": True,
        },
    )
    downgraded = panel.bearer(TRANSITIONS, {"target_mode": "SHADOW", "reason": "назад"})

    assert evidence["allowed"] is True and evidence["minimum_days_remaining"] == 0
    assert promoted.status_code == 200 and promoted.json()["mode"]["mode"] == "APPROVAL"
    assert downgraded.status_code == 200 and downgraded.json()["mode"]["mode"] == "SHADOW"
    journal = panel.get("/api/v1/mode").json()["transitions"]
    assert [(item["from_mode"], item["to_mode"]) for item in journal] == [
        ("APPROVAL", "SHADOW"),
        ("SHADOW", "APPROVAL"),
    ]


def test_live_promotion_without_live_evidence_is_refused(panel: Panel) -> None:
    promote_to_approval(panel.harness)

    response = panel.bearer(
        TRANSITIONS,
        {
            "target_mode": "LIVE",
            "reason": "live",
            "confirmation_id": "operator-confirmation",
            "operator_token": OPERATOR_TOKEN,
            "confirm": True,
        },
    )

    assert response.status_code == 409 and error_code(response) == "LIVE_EVIDENCE_MISSING"
    assert mode_state(panel)["mode"] == "APPROVAL"
    live = panel.get("/api/v1/promotion").json()["live"]
    assert live["ready"] is False
    assert [item["code"] for item in live["reasons"]] == ["LIVE_EVIDENCE_MISSING"]


def test_invalid_input_is_answered_with_the_stable_error_envelope(panel: Panel) -> None:
    empty_reason = panel.bearer(KILL, {"reason": "   "})
    unknown_field = panel.bearer(KILL, {"reason": "stop", "force": True})
    bad_limit = panel.get("/api/v1/decisions?limit=0")
    bad_cursor = panel.get("/api/v1/pools?cursor=-1")
    bad_address = panel.get("/api/v1/pools/not-a-key")
    bad_id = panel.get("/api/v1/decisions/not-a-uuid")
    missing = panel.get("/api/v1/decisions/00000000-0000-4000-8000-000000000000")

    for response in (empty_reason, unknown_field, bad_limit, bad_cursor, bad_address, bad_id):
        assert response.status_code == 422, response.text
        assert error_code(response) == "INVALID_INPUT"
        assert response.json()["error"]["details"]["fields"]
    assert missing.status_code == 404 and error_code(missing) == "DECISION_NOT_FOUND"
    assert mode_state(panel)["kill_switch_active"] is False
