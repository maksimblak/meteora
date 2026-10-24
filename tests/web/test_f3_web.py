"""F3: the panel never links off-site, survives malformed CSRF material and closes alerts."""

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from src.api.security import OperatorAuth
from src.core.clock import FrozenClock
from src.modes.models import CriticalAlert
from tests.api.support import OPERATOR_TOKEN, Panel, make_panel, operator_settings
from tests.worker.harness import NOW, POOL, spike_history


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[Panel]:
    instance = make_panel(tmp_path)
    yield instance
    instance.repositories.close()


def back_link(page: str) -> str:
    match = re.search(r'<a href="([^"]*)">Вернуться</a>', page)
    assert match is not None, "an error page offers a way back"
    return match.group(1)


def test_a_protocol_relative_path_never_becomes_an_off_site_back_link(panel: Panel) -> None:
    response = panel.get(httpx.URL("http://testserver//evil.example"))

    assert response.status_code == 404
    assert back_link(response.text) == "/"
    assert "evil.example" not in back_link(response.text)


def test_a_non_ascii_csrf_token_is_refused_not_a_server_error(panel: Panel) -> None:
    panel.login()

    response = panel.post("/kill-switch/activate", data={"reason": "стоп", "csrf_token": "тест"})

    assert response.status_code == 403
    assert "CSRF_FAILED" in response.text


def test_a_non_ascii_session_signature_is_no_session_not_a_crash() -> None:
    auth = OperatorAuth(operator_settings(), FrozenClock(NOW))

    assert auth.session_id("abc.123.подпись") is None


def test_a_plan_refused_at_admission_shows_why_in_russian(panel: Panel) -> None:
    harness = panel.harness
    harness.ingest(spike_history())

    async def run() -> Any:
        # The UTC risk day was never opened: the plan is recorded but not admitted.
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    page = panel.get(f"/decisions/{outcome.decision_id}").text

    assert "RISK_DAY_NOT_OPENED" in outcome.reasons
    assert "risk day ещё не открыт" in page
    assert "<code>RISK_DAY_NOT_OPENED</code>" in page


def raise_alert(panel: Panel) -> UUID:
    alert = CriticalAlert(
        alert_id=uuid4(),
        code="EXIT_REQUIRES_EXECUTION_GATEWAY",
        message="exit of position needs the gateway",
        created_at=NOW,
    )
    panel.repositories.mode.raise_alert(alert)
    return alert.alert_id


def test_the_dashboard_and_settings_show_each_open_alert_with_its_id(panel: Panel) -> None:
    alert_id = raise_alert(panel)

    anonymous = panel.get("/").text
    panel.login()
    pages = [panel.get(path).text for path in ("/", "/settings")]

    assert str(alert_id) in anonymous
    assert "выход позиции требует execution gateway" in anonymous
    for page in pages:
        assert str(alert_id) in page
        assert f'action="/alerts/{alert_id}/resolve"' in page
        assert 'name="operator_token"' in page and 'name="confirm"' in page


def test_the_resolve_form_needs_csrf_and_the_token_typed_again(panel: Panel) -> None:
    alert_id = raise_alert(panel)
    csrf = panel.login()
    path = f"/alerts/{alert_id}/resolve"

    forged = panel.post(path, data={"reason": "разобрано", "operator_token": OPERATOR_TOKEN,
                                    "confirm": "on"})
    no_token = panel.post(path, data={"reason": "разобрано", "confirm": "on", "csrf_token": csrf})
    closed = panel.post(path, data={"reason": "разобрано", "operator_token": OPERATOR_TOKEN,
                                    "confirm": "on", "csrf_token": csrf})

    assert forged.status_code == 403 and "CSRF_FAILED" in forged.text
    assert no_token.status_code == 403 and "REAUTHENTICATION_FAILED" in no_token.text
    assert back_link(no_token.text) == "/settings"
    assert closed.status_code == 303 and closed.headers["location"].startswith("/settings")
    assert str(alert_id) not in panel.get("/settings").text
