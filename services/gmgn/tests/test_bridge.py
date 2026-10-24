"""The browser bridge (G25): one tab, sequential requests, a per-minute limit, a cache per
token, a pause on any block - and every route behind the shared token.

The tab is replaced by ``FakeTab``, which answers each request from the live recording of
2026-10-24 (``fixtures/bridge_grok.json``); no browser is started here.
"""

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gmgn_service.bridge import (
    BridgeFetcher,
    BridgeSettings,
    MinuteLimiter,
    create_bridge_app,
)
from gmgn_service.config import ServiceSettings
from gmgn_service.contract import SignalsV2
from gmgn_service.service import PageUnavailable, SignalService
from gmgn_service.webapi import WebAnswer, WebRequest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "bridge_grok.json").read_text(encoding="utf-8")
)
GROK = FIXTURE["mint"]
OTHER = "CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump"
THIRD = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
START = datetime(2026, 10, 24, 16, 50, tzinfo=UTC)
TOKEN = "t21-test-bridge-token-0123456789"  # noqa: S105 - a test value
CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head></html>"
SOURCE = Path(__file__).resolve().parents[1] / "gmgn_service"


@dataclass
class Clock:
    wall: datetime = START
    seconds: float = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.seconds += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


@dataclass
class FakeTab:
    """The gmgn.ai tab: recorded answers by endpoint, scripted refusals, a request log."""

    clock: Clock
    refusals: dict[str, WebAnswer] = field(default_factory=dict)
    prepare_refusal: PageUnavailable | None = None
    login: str = "LOGGED_OUT"
    requests: list[tuple[float, WebRequest]] = field(default_factory=list)
    running: int = 0
    peak: int = 0
    blocked_marks: int = 0
    prepares: int = 0

    @property
    def ready(self) -> bool:
        return True

    async def prepare(self) -> None:
        self.prepares += 1
        if self.prepare_refusal is not None:
            raise self.prepare_refusal

    def mark_blocked(self) -> None:
        self.blocked_marks += 1

    async def refresh_login(self) -> str:
        return self.login

    async def request(self, request: WebRequest) -> WebAnswer:
        self.requests.append((self.clock.monotonic(), request))
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0)
            if request.name in self.refusals:
                return self.refusals[request.name]
            return WebAnswer(200, "application/json", json.dumps(self._recorded(request)))
        finally:
            self.running -= 1

    def _recorded(self, request: WebRequest) -> Any:
        answer = copy.deepcopy(FIXTURE["answers"][request.name])
        if request.name == "mutil_window_token_info":
            # Every address asked for gets the recorded item under its own address.
            (item,) = answer["data"]
            addresses = json.loads(request.body or "{}")["addresses"]
            answer["data"] = [{**item, "address": address} for address in addresses]
        if request.name == "token_fee_info":
            answer["data"]["address"] = request.path.rsplit("/", 1)[1]
        return answer

    def names(self) -> list[str]:
        return [request.name for _, request in self.requests]


def service_settings(**changes: Any) -> ServiceSettings:
    values: dict[str, Any] = {
        "cache_ttl": 600.0,
        "max_pages": 1,
        "response_wait": 5.0,
        "breaker_threshold": 3,
        "breaker_cooldown": 900.0,
    }
    values.update(changes)
    return ServiceSettings(**values)


def bridge(
    tab: FakeTab, clock: Clock, *, per_minute: int = 60, **changes: Any
) -> tuple[SignalService, BridgeFetcher]:
    fetcher = BridgeFetcher(
        tab,
        limiter=MinuteLimiter(per_minute, monotonic=clock.monotonic, sleep=clock.sleep),
        monotonic=clock.monotonic,
    )
    service = SignalService(
        fetcher,
        settings=service_settings(**changes),
        schema=SignalsV2,
        now=clock.now,
        monotonic=clock.monotonic,
    )
    return service, fetcher


def run(work: Any) -> Any:
    return asyncio.run(work())


def test_a_token_is_read_in_the_tab_one_request_at_a_time_as_a_v2_answer() -> None:
    clock = Clock()
    tab = FakeTab(clock)
    service, _ = bridge(tab, clock)

    answer = run(lambda: service.signals("sol", GROK))

    assert answer.schema_version == "gmgn-signals.v2"
    assert (answer.status, answer.freshness, answer.source_at) == ("AVAILABLE", "FRESH", START)
    assert answer.signals.insiders_share == Decimal("0.7968")
    assert answer.signals.volume_1h_usd == Decimal("137356.48892064")
    assert answer.signals.sniper_count == 20
    assert answer.signals.developer_status == "SOLD"
    assert answer.missing == []
    assert tab.names() == [
        "mutil_window_token_info",
        "token_stat",
        "token_holder_stat",
        "token_fee_info",
        "token_mcap_candles",
        "trending_multichain",
    ]
    assert tab.peak == 1


def test_tokens_waiting_together_share_one_batch_and_one_trending_request() -> None:
    clock = Clock()
    tab = FakeTab(clock)
    service, _ = bridge(tab, clock)

    async def work() -> list[Any]:
        return list(await asyncio.gather(*(service.signals("sol", m) for m in (GROK, OTHER))))

    answers = run(work)

    assert [answer.status for answer in answers] == ["AVAILABLE", "AVAILABLE"]
    batches = [request for _, request in tab.requests if request.name == "mutil_window_token_info"]
    assert len(batches) == 1
    assert json.loads(batches[0].body or "")["addresses"] == [GROK, OTHER]
    assert tab.names().count("trending_multichain") == 1
    assert tab.names().count("token_stat") == 2
    assert tab.peak == 1  # the two tokens were read one after the other in the one tab


def test_the_requests_per_minute_never_exceed_the_limit() -> None:
    clock = Clock()
    tab = FakeTab(clock)
    service, _ = bridge(tab, clock, per_minute=5)

    run(lambda: service.signals("sol", GROK))

    times = [moment - 1000.0 for moment, _ in tab.requests]
    assert len(times) == 6
    assert times[:5] == [0.0] * 5
    assert times[5] == pytest.approx(60.0)
    for index in range(len(times) - 5):
        assert times[index + 5] - times[index] >= 60.0


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (WebAnswer(429, "text/html; charset=UTF-8", CHALLENGE_PAGE), "RATE_LIMITED"),
        (WebAnswer(403, "text/html; charset=UTF-8", CHALLENGE_PAGE), "CHALLENGE"),
        (WebAnswer(403, "text/html", "<html>you have been blocked</html>"), "ACCESS_BLOCKED"),
    ],
    ids=["429", "challenge", "block"],
)
def test_a_block_pauses_every_token_at_once_and_is_never_repeated(
    refusal: WebAnswer, reason: str
) -> None:
    clock = Clock()
    tab = FakeTab(clock, refusals={"token_stat": refusal})
    service, _ = bridge(tab, clock, breaker_threshold=3, breaker_cooldown=900.0)

    async def work() -> list[Any]:
        first = await service.signals("sol", GROK)
        sent = len(tab.requests)
        second = await service.signals("sol", OTHER)
        ready = service.readiness()
        paused = service.paused()
        clock.advance(901)
        tab.refusals.clear()
        probe = await service.signals("sol", OTHER)
        return [first, sent, second, ready, paused, probe]

    first, sent, second, ready, paused, probe = run(work)

    assert (first.status, first.reason) == ("UNAVAILABLE", reason)
    # The load stopped at the block: nothing after token_stat was asked.
    assert sent == 2
    assert (second.status, second.reason) == ("UNAVAILABLE", reason)
    assert ready == (False, ["CIRCUIT_OPEN"])
    assert paused == (reason, START + timedelta(seconds=900))
    assert tab.blocked_marks == 1
    assert probe.status == "AVAILABLE"
    assert len([1 for _, item in tab.requests if item.name == "token_stat"]) == 2


def test_tokens_already_waiting_when_a_block_starts_send_nothing_during_the_pause() -> None:
    """Loads queued behind the one that met the 429 neither reload the tab nor ask gmgn.ai:
    they answer UNAVAILABLE with the pause's reason."""

    clock = Clock()
    rate_limited = WebAnswer(429, "text/html; charset=UTF-8", CHALLENGE_PAGE)
    tab = FakeTab(clock, refusals={"token_stat": rate_limited})
    service, _ = bridge(tab, clock, max_pending=16)

    async def work() -> list[Any]:
        return list(
            await asyncio.gather(*(service.signals("sol", mint) for mint in (GROK, OTHER, THIRD)))
        )

    answers = run(work)

    assert [(item.status, item.reason) for item in answers] == [("UNAVAILABLE", "RATE_LIMITED")] * 3
    assert tab.prepares == 1
    assert tab.names() == ["mutil_window_token_info", "token_stat"]
    assert tab.blocked_marks == 1
    assert service.paused() == ("RATE_LIMITED", START + timedelta(seconds=900))


def test_a_captcha_on_the_page_sends_no_request_and_pauses() -> None:
    clock = Clock()
    tab = FakeTab(clock, prepare_refusal=PageUnavailable("CAPTCHA", pause=True))
    service, _ = bridge(tab, clock)

    answer = run(lambda: service.signals("sol", GROK))

    assert (answer.status, answer.reason) == ("UNAVAILABLE", "CAPTCHA")
    assert tab.requests == []
    assert service.paused() is not None


def test_a_login_wall_is_login_required_and_does_not_pause_other_tokens() -> None:
    clock = Clock()
    wall = WebAnswer(401, "application/json", '{"code":401,"message":"unauthorized"}')
    names = (
        "mutil_window_token_info",
        "token_stat",
        "token_holder_stat",
        "token_fee_info",
        "token_mcap_candles",
        "trending_multichain",
    )
    tab = FakeTab(clock, refusals=dict.fromkeys(names, wall))
    service, _ = bridge(tab, clock, breaker_threshold=5)

    async def work() -> list[Any]:
        return [await service.signals("sol", GROK), await service.signals("sol", OTHER)]

    first, second = run(work)

    assert (first.status, first.reason) == ("UNAVAILABLE", "LOGIN_REQUIRED")
    assert (second.status, second.reason) == ("UNAVAILABLE", "LOGIN_REQUIRED")
    assert service.paused() is None
    assert tab.names().count("token_stat") == 2


def test_a_cached_token_is_served_without_touching_the_tab() -> None:
    clock = Clock()
    tab = FakeTab(clock)
    service, _ = bridge(tab, clock, cache_ttl=300.0)

    async def work() -> list[Any]:
        first = await service.signals("sol", GROK)
        sent = len(tab.requests)
        clock.advance(299)
        second = await service.signals("sol", GROK)
        return [first, sent, second, len(tab.requests)]

    first, sent, second, total = run(work)

    assert second.source_at == first.source_at == START
    assert total == sent


def app_client(tab: FakeTab, clock: Clock) -> TestClient:
    service, _ = bridge(tab, clock)
    return TestClient(create_bridge_app(token=SecretStr(TOKEN), service=service, tab=tab))


def test_every_route_of_the_bridge_needs_the_shared_token() -> None:
    clock = Clock()
    tab = FakeTab(clock)

    with app_client(tab, clock) as http:
        path = f"/v1/tokens/sol/{GROK}/signals"
        anonymous = [http.get(path), http.get("/health/live"), http.get("/health/ready")]
        wrong = http.get(path, headers={"Authorization": "Bearer not-the-token"})
        sent_before = len(tab.requests)
        good = http.get(path, headers={"Authorization": f"Bearer {TOKEN}"})

    assert [item.status_code for item in anonymous] == [401, 401, 401]
    assert wrong.status_code == 401
    assert TOKEN not in wrong.text
    assert sent_before == 0
    assert good.status_code == 200
    body = good.json()
    assert body["schema_version"] == "gmgn-signals.v2"
    assert body["signals"]["total_fees_sol"] == "0.9718177443108398"
    assert body["signals"]["created_at"] == "2026-10-24T15:56:15Z"
    assert body["signals"]["wash_trading"] is False


def test_readiness_says_whether_the_user_is_signed_in_without_any_cookie() -> None:
    clock = Clock()
    tab = FakeTab(clock, login="LOGGED_IN")

    with app_client(tab, clock) as http:
        ready = http.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "reasons": [],
        "mode": "bridge",
        "login": "LOGGED_IN",
        "paused": None,
    }


def environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    values = {"GMGN_BRIDGE_TOKEN": TOKEN, "GMGN_BRIDGE_PROFILE_DIR": str(tmp_path / "profile")}
    values.update(changes)
    return values


def test_the_bridge_listens_on_loopback_or_a_private_docker_address_only(tmp_path: Path) -> None:
    default = BridgeSettings.from_environment(environment(tmp_path))
    docker = BridgeSettings.from_environment(
        environment(tmp_path, GMGN_BRIDGE_HOST="127.0.0.1, 172.17.0.1", GMGN_BRIDGE_PORT="9000")
    )

    assert (default.hosts, default.port) == (("127.0.0.1",), 8765)
    assert (docker.hosts, docker.port) == (("127.0.0.1", "172.17.0.1"), 9000)
    assert default.service.max_pages == 1
    for host in ("0.0.0.0", "::", "8.8.8.8", "gmgn.example.org"):  # noqa: S104
        with pytest.raises(ValueError, match="GMGN_BRIDGE_HOST"):
            BridgeSettings.from_environment(environment(tmp_path, GMGN_BRIDGE_HOST=host))


def test_the_bridge_needs_a_long_shared_token_and_never_echoes_it(tmp_path: Path) -> None:
    missing = environment(tmp_path)
    del missing["GMGN_BRIDGE_TOKEN"]
    secret_file = tmp_path / "token.txt"
    secret_file.write_text(TOKEN + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="GMGN_BRIDGE_TOKEN") as absent:
        BridgeSettings.from_environment(missing)
    with pytest.raises(ValueError, match="GMGN_BRIDGE_TOKEN") as short:
        BridgeSettings.from_environment(
            environment(tmp_path, GMGN_BRIDGE_TOKEN="short")  # noqa: S106 - a test value
        )
    with pytest.raises(ValueError, match="not both"):
        BridgeSettings.from_environment(
            environment(tmp_path, GMGN_BRIDGE_TOKEN_FILE=str(secret_file))
        )
    from_file = BridgeSettings.from_environment(
        {**missing, "GMGN_BRIDGE_TOKEN_FILE": str(secret_file)}
    )

    assert "short" not in str(short.value) and TOKEN not in str(absent.value)
    assert from_file.token.get_secret_value() == TOKEN
    assert TOKEN not in repr(from_file)


def test_the_browser_profile_lives_outside_the_repository(tmp_path: Path) -> None:
    inside = SOURCE.parent / "profile"
    windows = BridgeSettings.default_profile(
        platform="win32", environ={"LOCALAPPDATA": str(tmp_path / "Local")}, home=tmp_path
    )
    linux = BridgeSettings.default_profile(platform="linux", environ={}, home=tmp_path)

    with pytest.raises(ValueError, match="GMGN_BRIDGE_PROFILE_DIR"):
        BridgeSettings.from_environment(environment(tmp_path, GMGN_BRIDGE_PROFILE_DIR=str(inside)))
    assert windows == tmp_path / "Local" / "MeteoraVolumeBot" / "gmgn-bridge-profile"
    assert linux == tmp_path / ".local" / "share" / "meteora-volume-bot" / "gmgn-bridge-profile"


def test_the_bridge_window_is_plain_visible_playwright_chromium() -> None:
    """Spec 6.7/G25 boundaries as a guard on the service source: the official Chromium of
    Playwright in a visible window with a persistent profile and nothing else changed."""

    tab = (SOURCE / "tab.py").read_text(encoding="utf-8")
    source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py")).lower()

    (arguments,) = re.findall(r"launch_persistent_context\(([^)]*)\)", tab)
    keywords = re.findall(r"(\w+)\s*=", arguments)
    assert keywords == ["headless"] and "headless=False" in arguments
    for forbidden in (
        "stealth",
        "user_agent",
        "add_init_script",
        "storage_state",
        "solver",
        "2captcha",
        "webdriver",
        "cookies(",
        "add_cookies",
        "extra_http_headers",
        "ignore_default_args",
        "bypass_csp",
        "--disable",
        ".route(",
    ):
        assert forbidden not in source, forbidden
    assert not re.search(r"\bargs\s*=", source)
    assert "headless=true" not in source
