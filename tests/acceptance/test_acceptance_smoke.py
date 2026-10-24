"""Acceptance smoke of the delivered stack (ticket 08): Compose up, shadow cycle, restart.

Opt-in: ``ACCEPTANCE_COMPOSE=1`` (``scripts/acceptance-smoke.ps1`` sets it). The canonical
check skips it, like the other integration tests that need a running service.

The stack is ``docker-compose.yml`` plus ``tests/acceptance/compose.acceptance.yml`` under
its own project name (``meteora-acceptance``) and its own api port, with
``tests/acceptance/acceptance.env`` instead of ``.env``: SHADOW, no Solana RPC, no wallet,
no executor secrets. The deployed worker syncs recorded live sweeps from ``fixture-api``
(no external network), so the checks below are the delivered processes end to end:

1. migrations at head and every service healthy;
2. the fixture universe ingested through the real Data API adapter;
3. a candidate rejected by a hard filter (USDC with a live freeze authority) and by the
   guide-v1 pair and theme rules (USDC/SOL is two quote tokens, TRUMP an excluded theme);
4. a shadow position opened, kept across a worker/api restart and closed exactly once;
5. the web panel and the API, operator login, CSRF and the kill switch;
6. state (kill switch, positions, decisions) after a full stop and start, worker resumed.

No transaction is built or sent: nothing in the stack holds a wallet.
"""

import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = os.environ.get("ACCEPTANCE_PROJECT", "meteora-acceptance")
PORT = int(os.environ.get("ACCEPTANCE_API_PORT", "18080"))
KEEP = os.environ.get("ACCEPTANCE_KEEP") == "1"
FIXTURE_POOLS = 17
USDC_SOL = "HRYEjwdo3bZ1TpXKWKcezqiwSV2Ywuh4LxMa2PzoCnG6"
TRUMP_USDC = "3C5YE97HADPDxZehYq9Cis8AXr9aNyrUsczKzE1nDbW9"
COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "--project-directory",
    str(ROOT),
    "--env-file",
    str(ROOT / "tests" / "acceptance" / "acceptance.env"),
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-f",
    str(ROOT / "tests" / "acceptance" / "compose.acceptance.yml"),
]

pytestmark = pytest.mark.skipif(
    os.environ.get("ACCEPTANCE_COMPOSE") != "1",
    reason="acceptance smoke needs Docker: run scripts/acceptance-smoke.ps1 (ACCEPTANCE_COMPOSE=1)",
)


@dataclass
class Stack:
    token: str
    environment: dict[str, str]
    client: httpx.Client
    notes: dict[str, Any] = field(default_factory=dict)

    def compose(self, *arguments: str, timeout: float = 900) -> str:
        result = subprocess.run(  # noqa: S603 - fixed docker argv, no shell
            [*COMPOSE, *arguments],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"docker compose {' '.join(arguments)} failed ({result.returncode}):\n"
                f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
            )
        return result.stdout

    def get(self, path: str, **options: Any) -> httpx.Response:
        return self.client.get(path, **options)

    def json(self, path: str) -> Any:
        response = self.client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:500])
        return response.json()


def _environment(token: str) -> dict[str, str]:
    """The test process environment without any variable the stack would interpolate."""

    names = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    environment = {key: value for key, value in os.environ.items() if key not in names}
    environment.update(
        {
            "APP_OPERATOR_TOKEN": token,
            "ACCEPTANCE_API_PORT": str(PORT),
            "COMPOSE_PROJECT_NAME": PROJECT,
        }
    )
    return environment


def wait_for(
    description: str, probe: Callable[[], Any], *, timeout: float, interval: float = 3.0
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (httpx.HTTPError, AssertionError, KeyError, ValueError) as error:
            last = error
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {description}; last observation: {last!r}")


def migration_head() -> str:
    revisions: dict[str, str | None] = {}
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "([^"]+)"', text, flags=re.MULTILINE)
        parent = re.search(r'^down_revision: str \| None = "([^"]+)"', text, flags=re.MULTILINE)
        if revision:
            revisions[revision.group(1)] = parent.group(1) if parent else None
    (head,) = set(revisions) - {parent for parent in revisions.values() if parent}
    return head


def positions(stack: Stack) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = stack.json("/api/v1/positions?limit=200")["items"]
    return items


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    token = secrets.token_urlsafe(32)  # throwaway, lives only in this stack
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=10.0)
    current = Stack(token=token, environment=_environment(token), client=client)
    current.compose("down", "-v", "--remove-orphans", timeout=300)
    try:
        current.compose("up", "-d", "--build", "--wait", "--wait-timeout", "600", timeout=1800)
        yield current
    finally:
        client.close()
        if not KEEP:
            current.compose("down", "-v", "--remove-orphans", timeout=300)


def test_migrations_reach_head_and_every_service_is_healthy(stack: Stack) -> None:
    migrate = json.loads(
        stack.compose("ps", "-a", "--format", "json", "migrate").strip().splitlines()[0]
    )
    assert migrate["State"] == "exited" and migrate["ExitCode"] == 0
    version = stack.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "meteora",
        "-d",
        "meteora",
        "-Atc",
        "select version_num from alembic_version",
    ).strip()
    assert version == migration_head()

    assert stack.get("/health/live").status_code == 200
    assert stack.get("/health/ready").json() == {"status": "ready", "mode": "SHADOW"}
    assert "meteora_api_request_seconds" in stack.get("/metrics").text

    def worker_seen() -> bool:
        services = {item["name"]: item for item in stack.json("/api/v1/system/status")["services"]}
        # DEGRADED, not DOWN: SHADOW keeps observing without RPC (the solana provider).
        return services["worker"]["status"] in {"OK", "DEGRADED"}

    wait_for("worker health through the api", worker_seen, timeout=120)
    status = stack.json("/api/v1/system/status")
    assert status["mode"]["mode"] == "SHADOW" and status["mode"]["kill_switch_active"] is False


def test_the_fixture_universe_is_ingested_by_the_deployed_worker(stack: Stack) -> None:
    def fresh() -> dict[str, Any] | None:
        universe = stack.json("/api/v1/system/status")["universe"]
        ok = universe["state"] == "FRESH" and universe["pool_count"] == FIXTURE_POOLS
        return universe if ok else None

    universe = wait_for("the first universe sync", fresh, timeout=240)
    pools = stack.json("/api/v1/pools?limit=200")["items"]
    assert len(pools) == FIXTURE_POOLS
    assert USDC_SOL in {pool["address"] for pool in pools}
    stack.notes["first_sync_at"] = universe["last_sync_at"]


def test_a_candidate_is_rejected_by_a_hard_filter(stack: Stack) -> None:
    def rejected() -> dict[str, Any] | None:
        page = stack.json(f"/api/v1/decisions?status=REJECT&pool={USDC_SOL}&limit=5")
        return page["items"][0] if page["items"] else None

    decision = wait_for("a REJECT decision for USDC/SOL", rejected, timeout=240)
    codes = {reason["code"] for reason in decision["reasons"]}
    assert "FREEZE_AUTHORITY_PRESENT" in codes
    assert "PAIR_OF_QUOTE_TOKENS" in codes  # F5: two quote tokens are no coin to provide for
    assert decision["mode"] == "SHADOW" and decision["ruleset_version"] == "guide-v1"
    detail = stack.json(f"/api/v1/decisions/{decision['decision_id']}")
    assert detail  # the full decision record (facts, thresholds, evidence) is served

    # The theme filter of guide-v1 (spec 6.8) on a recorded pool: TRUMP is an excluded word.
    def theme_rejected() -> dict[str, Any] | None:
        page = stack.json(f"/api/v1/decisions?status=REJECT&pool={TRUMP_USDC}&limit=5")
        return page["items"][0] if page["items"] else None

    trump = wait_for("a REJECT decision for TRUMP-USDC", theme_rejected, timeout=240)
    assert "THEME_EXCLUDED" in {reason["code"] for reason in trump["reasons"]}


def test_a_shadow_position_survives_a_restart_and_closes_once(stack: Stack) -> None:
    opened = wait_for(
        "a shadow position to open",
        lambda: [item for item in positions(stack) if item["status"] == "OPEN"],
        timeout=600,
        interval=5,
    )
    tracked = {item["position_id"] for item in opened}
    assert all(item["mode"] == "SHADOW" for item in opened)

    stack.compose("restart", "worker", "api", timeout=300)
    wait_for(
        "the api after the restart",
        lambda: stack.get("/health/ready").status_code == 200,
        timeout=180,
    )
    after_restart = {item["position_id"]: item for item in positions(stack)}
    assert tracked <= after_restart.keys(), "an open position was lost by the restart"
    assert len(after_restart) <= 3  # the three-position cap holds across the restart

    def all_closed() -> list[dict[str, Any]] | None:
        current = positions(stack)
        mine = [item for item in current if item["position_id"] in tracked]
        return mine if mine and all(item["status"] == "CLOSED" for item in mine) else None

    closed = wait_for("the tracked positions to close", all_closed, timeout=720, interval=5)
    for item in closed:
        detail = stack.json(f"/api/v1/positions/{item['position_id']}")
        kinds = [step["kind"] for step in detail["lifecycle"]]
        assert kinds.count("OPEN") == 1 and kinds.count("CLOSE") == 1, kinds
        assert detail["position"]["valuation_source"] == "OUTCOME"
        assert detail["position"]["hodl_pnl_base_units"] is not None
    stack.notes["closed"] = sorted(tracked)


def test_web_panel_and_api_with_operator_login_csrf_and_kill_switch(stack: Stack) -> None:
    for path in (
        "/",
        "/pools",
        "/decisions",
        "/positions",
        "/performance",
        "/settings",
        "/mode",
        "/approvals",
        "/login",
        "/docs",
    ):
        response = stack.get(path)
        assert response.status_code == 200, (path, response.status_code)
    assert "SHADOW" in stack.get("/").text
    position = stack.notes["closed"][0]
    assert stack.get(f"/positions/{position}").status_code == 200

    effective = stack.get("/api/v1/config/effective").text
    assert stack.token not in effective and "[REDACTED]" in effective

    # Writes are closed without the operator.
    anonymous = stack.client.post(
        "/api/v1/kill-switch/activate", json={"reason": "acceptance: anonymous"}
    )
    assert anonymous.status_code == 401
    wrong = stack.client.post("/login", data={"token": "not-the-token", "next": "/mode"})
    assert wrong.status_code == 401

    with httpx.Client(base_url=str(stack.client.base_url), timeout=10.0) as browser:
        login = browser.post("/login", data={"token": stack.token, "next": "/mode"})
        assert login.status_code == 303 and "mvb_session" in browser.cookies
        page = browser.get("/mode").text
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert match, "the mode page carries the session CSRF token"
        forged = browser.post("/kill-switch/activate", data={"reason": "acceptance: no csrf"})
        assert forged.status_code == 403
        activated = browser.post(
            "/kill-switch/activate",
            data={
                "csrf_token": match.group(1),
                "reason": "acceptance: kill switch from the panel",
                "policy": "HOLD_AND_ALERT",
            },
        )
        assert activated.status_code == 303
    state = stack.json("/api/v1/system/status")
    assert state["mode"]["kill_switch_active"] is True and state["mode"]["mode"] == "SHADOW"
    assert "KILL_SWITCH_ACTIVE" in {alert["code"] for alert in state["alerts"] if alert["open"]}


def test_state_survives_a_full_stop_and_the_worker_resumes(stack: Stack) -> None:
    before = stack.json("/api/v1/system/status")
    known_positions = {item["position_id"]: item["status"] for item in positions(stack)}
    decisions_before = len(stack.json("/api/v1/decisions?limit=200")["items"])

    stack.compose("stop", timeout=300)
    stack.compose("up", "-d", "--wait", "--wait-timeout", "300", timeout=900)

    after = stack.json("/api/v1/system/status")
    assert after["mode"]["kill_switch_active"] is True
    assert after["mode"]["mode"] == "SHADOW"
    assert after["mode"]["version"] == before["mode"]["version"]
    assert {item["position_id"]: item["status"] for item in positions(stack)} == known_positions
    assert len(stack.json("/api/v1/decisions?limit=200")["items"]) >= decisions_before

    def synced_again() -> bool:
        universe = stack.json("/api/v1/system/status")["universe"]
        return bool(universe["last_sync_at"]) and universe["last_sync_at"] > (
            before["universe"]["last_sync_at"] or ""
        )

    wait_for("a universe sync after the restart", synced_again, timeout=240)

    headers = {"Authorization": f"Bearer {stack.token}"}
    refused = stack.client.post(
        "/api/v1/kill-switch/release",
        json={"reason": "acceptance: release without re-auth"},
        headers=headers,
    )
    assert refused.status_code == 403
    released = stack.client.post(
        "/api/v1/kill-switch/release",
        json={"reason": "acceptance: release", "operator_token": stack.token, "confirm": True},
        headers=headers,
    )
    assert released.status_code == 200, released.text
    assert released.json()["kill_switch_active"] is False
