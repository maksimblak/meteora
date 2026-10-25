"""Russian server-rendered panel: pages, login, CSRF-protected forms, refresh safety."""

import json
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
    # An entry's two marks of spec 6.8 (G23).
    "organic_chart": "on",
    "theme_not_excluded": "on",
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
    # One stream per page: the overview's own stream also carries the status pulse, so an
    # overview tab holds one of the browser's six HTTP/1.1 connections per host, not two.
    assert text.count("data-stream=") == 1
    assert 'data-stream="/stream/overview"' in text
    assert 'data-stream="/api/v1/stream/status"' in panel.get("/pools").text
    assert OPERATOR_TOKEN not in text


def stream_events(text: str) -> dict[str, str]:
    """``event: <name>`` -> its ``data:`` line, from a recorded SSE body."""

    found: dict[str, str] = {}
    name = ""
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            found[name] = line.removeprefix("data: ")
    return found


def test_the_overview_tells_a_viewer_what_is_happening_now(panel: Panel) -> None:
    scenario = populated(panel.harness)

    text = panel.get("/").text

    assert "Что происходит сейчас" in text
    assert "реальные деньги не тратятся" in text  # SHADOW in plain words
    assert "Последние решения бота" in text
    assert f'href="/decisions/{scenario.accepted.decision_id}"' in text


def test_the_overview_stream_pushes_status_and_the_same_parts_without_a_reload(
    panel: Panel,
) -> None:
    scenario = populated(panel.harness)

    response = panel.get("/stream/overview")
    events = stream_events(response.text)
    parts = json.loads(events["overview"])
    page = panel.get("/").text

    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"universe_state":"FRESH"' in events["status"]
    assert set(parts) == {"now", "feed"}
    assert "реальные деньги не тратятся" in parts["now"]
    assert f'href="/decisions/{scenario.accepted.decision_id}"' in parts["feed"]
    # The stream renders with the page's own macros: what it pushes is what the page shows.
    assert parts["feed"] in page


def test_the_feed_shows_how_long_ago_not_a_bare_clock_time(panel: Panel) -> None:
    populated(panel.harness)

    feed = json.loads(stream_events(panel.get("/stream/overview").text)["overview"])["feed"]

    # Decided at NOW and read at NOW: a viewer sees "0 с назад" and the full time on hover.
    assert "0 с назад" in feed
    assert f'title="{NOW:%Y-%m-%d %H:%M:%S} UTC"' in feed


def test_before_the_first_sync_the_overview_says_the_bot_is_starting(panel: Panel) -> None:
    now = json.loads(stream_events(panel.get("/stream/overview").text)["overview"])["now"]

    assert "Запуск" in now
    assert "реальные деньги не тратятся" not in now  # it is not watching the market yet


def test_with_the_kill_switch_on_the_overview_says_the_bot_is_stopped(panel: Panel) -> None:
    populated(panel.harness)
    csrf = panel.login()
    panel.post("/kill-switch/activate", data={"reason": "стоп", "csrf_token": csrf})

    now = json.loads(stream_events(panel.get("/stream/overview").text)["overview"])["now"]

    assert "Остановлен" in now
    assert "реальные деньги не тратятся" not in now


def test_in_shadow_the_approval_queue_explains_calmly_and_folds_what_is_missing(
    panel: Panel,
) -> None:
    page = panel.get("/approvals").text

    # SHADOW has nothing to approve: no alarm, a plain note, the APPROVAL gaps folded away.
    assert "появятся в режиме APPROVAL" in page
    assert '<details class="missing">' in page
    assert "<code>MODE_NOT_APPROVAL</code>" in page.split('<details class="missing">', 1)[1]
    assert "Почему команды сейчас не готовы к подтверждению" not in page


def test_in_approval_the_queue_still_warns_why_commands_are_not_ready(
    approval: tuple[Panel, ApprovalCase],
) -> None:
    panel, _ = approval

    page = panel.get("/approvals").text

    assert "появятся в режиме APPROVAL" not in page


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
        "График органичный",
        "Тема не Илон/Трамп/политика/стрим",
        "Издержки",
        "План и команда",
        "Подтверждение будет действовать до",
    ):
        assert fragment in page
    # Before an irreversible on-chain operation the addresses are shown whole: a 4…4
    # abbreviation is exactly what a look-alike (address-poisoning) address imitates.
    assert f"<code>{POOL.address.root}</code>" in page
    assert 'name="organic_chart" required' in page
    assert 'name="theme_not_excluded" required' in page
    form = {
        "csrf_token": csrf,
        "approval_id": hidden(page, "approval_id"),
        "command_digest": hidden(page, "command_digest"),
        "simulation_digest": hidden(page, "simulation_digest"),
        "operator_token": OPERATOR_TOKEN,
        "confirm_irreversible": "on",
    }
    g17_only = {
        key: value
        for key, value in CHECKLIST.items()
        if key not in {"organic_chart", "theme_not_excluded"}
    }

    incomplete = panel.post(path, data={**form, "twitter_hype": "on"})
    without_chart_and_theme = panel.post(path, data={**form, **g17_only})
    approved = panel.post(path, data={**form, **CHECKLIST})
    resent = panel.post(path, data={**form, **CHECKLIST})
    refreshed = panel.get(approved.headers["location"])

    assert incomplete.status_code == 422 and "Отметьте все пункты" in incomplete.text
    assert without_chart_and_theme.status_code == 422
    assert approved.status_code == 303
    assert approved.headers["location"] == f"{path}?done=approved"
    assert resent.status_code == 303
    assert refreshed.status_code == 200
    assert "подтверждено, ждёт отправки" in refreshed.text
    assert 'name="approval_id"' not in refreshed.text
    assert panel.get("/api/v1/approvals").json()["pending"] == []
    phases = {item["phase"] for item in panel.get("/api/v1/approvals").json()["recent"]}
    assert phases == {ExecutionPhase.APPROVED.value}
