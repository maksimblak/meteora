"""Russian server-rendered panel: pages, login, CSRF-protected forms, refresh safety."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.core.clock import FrozenClock
from src.domain.screening.models import DecisionFlag
from src.execution.contracts import ExecutionPhase
from tests.api.support import (
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
from tests.worker.harness import NOW, POOL

CHECKLIST = {
    "twitter_hype": "on",
    "developer_supply": "on",
    "no_pvp_copies": "on",
    "bubblemaps_clusters": "on",
}


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[Panel]:
    instance = make_panel(tmp_path)
    yield instance
    instance.repositories.close()


@pytest.fixture
def approval(tmp_path: Path) -> Iterator[tuple[Panel, ApprovalCase]]:
    repositories = factory_repositories(tmp_path)
    harness = harness_over(repositories, FrozenClock(NOW))
    scenario = populated(harness)
    flag_decision(harness, scenario.accepted.decision_id, DecisionFlag.TOTAL_FEES_UNAVAILABLE)
    promote_to_approval(harness)
    case = awaiting_approval(scenario, repositories)
    instance = make_panel(
        tmp_path, repositories=repositories, harness=harness, approvals=case.setup
    )
    yield instance, case
    repositories.close()


def hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match is not None, name
    return match.group(1)


def test_first_screen_shows_services_mode_kill_switch_and_warm_up(panel: Panel) -> None:
    page = panel.get("/")

    assert page.status_code == 200
    text = page.text
    assert '<html lang="ru">' in text
    for fragment in (
        "Режим",
        "SHADOW",
        "Kill switch",
        "Сервисы",
        "database",
        "Первая синхронизация universe ещё не завершена",
        'href="/settings"',
        'href="/decisions"',
    ):
        assert fragment in text
    assert 'data-stream="/api/v1/stream/status"' in text
    assert OPERATOR_TOKEN not in text


def test_every_screen_renders_in_russian_with_data(panel: Panel) -> None:
    scenario = populated(panel.harness)
    position_id = scenario.accepted.entry.position_id

    pages = {
        "/pools": "DLMM-пулы",
        f"/pools/{POOL.address.root}": "Готовность истории",
        "/decisions": "Решения",
        f"/decisions/{scenario.accepted.decision_id}": "Причина выбора",
        "/positions": "Позиции",
        f"/positions/{position_id}": "Жизненный цикл",
        "/performance": "сравнение с HODL",
        "/settings": "Секреты (только наличие)",
        "/approvals": "Очередь подтверждений",
        "/mode": "Допуск в APPROVAL",
        "/login": "Вход оператора",
    }
    for path, heading in pages.items():
        response = panel.get(path)
        assert response.status_code == 200, path
        assert heading in response.text, path
    missing = panel.get("/decisions/00000000-0000-4000-8000-000000000000")
    assert missing.status_code == 404 and "Решение не найдено" in missing.text


def test_login_sets_an_http_only_strict_session_and_rejects_a_wrong_token(panel: Panel) -> None:
    wrong = panel.post("/login", data={"token": "typo", "next": "/"})
    right = panel.post("/login", data={"token": OPERATOR_TOKEN, "next": "//evil.example"})

    assert wrong.status_code == 401 and "Неверный токен" in wrong.text
    assert right.status_code == 303 and right.headers["location"] == "/"
    cookie = right.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert OPERATOR_TOKEN not in right.headers["set-cookie"]


def test_web_forms_need_a_session_and_its_csrf_token(panel: Panel) -> None:
    anonymous = panel.post("/kill-switch/activate", data={"reason": "стоп"})
    panel.login()
    forged = panel.post("/kill-switch/activate", data={"reason": "стоп"})
    page = panel.get("/mode").text
    allowed = panel.post(
        "/kill-switch/activate", data={"reason": "стоп", "csrf_token": hidden(page, "csrf_token")}
    )

    assert anonymous.status_code == 401
    assert forged.status_code == 403 and "CSRF" in forged.text
    assert allowed.status_code == 303 and allowed.headers["location"].startswith("/mode")
    assert "Снять kill switch" in panel.get("/mode").text


def test_approval_page_shows_checklist_flags_irreversibility_and_survives_refresh(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = approval
    path = f"/approvals/{case.command.command_id}"
    csrf = panel.login()
    page = panel.get(path).text

    for fragment in (
        "Необратимо",
        "total fees: нет данных — проверь в GMGN",
        "Twitter и хайп проверены",
        "Запас монет у девелопера проверен",
        "Нет гонки копий монеты (PvP)",
        "Кластеры на Bubblemaps проверены",
        "Издержки",
        "План и команда",
        "Подтверждение будет действовать до",
    ):
        assert fragment in page
    form = {
        "csrf_token": csrf,
        "approval_id": hidden(page, "approval_id"),
        "command_digest": hidden(page, "command_digest"),
        "simulation_digest": hidden(page, "simulation_digest"),
        "operator_token": OPERATOR_TOKEN,
        "confirm_irreversible": "on",
    }

    incomplete = panel.post(path, data={**form, "twitter_hype": "on"})
    approved = panel.post(path, data={**form, **CHECKLIST})
    resent = panel.post(path, data={**form, **CHECKLIST})
    refreshed = panel.get(approved.headers["location"])

    assert incomplete.status_code == 422 and "Отметьте все пункты" in incomplete.text
    assert approved.status_code == 303
    assert approved.headers["location"] == f"{path}?done=approved"
    assert resent.status_code == 303
    assert refreshed.status_code == 200
    assert "подтверждено, ждёт отправки" in refreshed.text
    assert 'name="approval_id"' not in refreshed.text
    assert panel.get("/api/v1/approvals").json()["pending"] == []
    phases = {item["phase"] for item in panel.get("/api/v1/approvals").json()["recent"]}
    assert phases == {ExecutionPhase.APPROVED.value}
