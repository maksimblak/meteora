"""Default wiring of ``create_operator_app``: factory repositories, T01 health, approvals
gated by the deployment's own configuration (never silently enabled)."""

import asyncio
from pathlib import Path

import httpx

from src.api.app import create_operator_app


def call(app: object, *paths: str) -> list[httpx.Response]:
    async def run() -> list[httpx.Response]:
        async with app.router.lifespan_context(app):  # type: ignore[attr-defined]
            transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return [await client.get(path) for path in paths]

    return asyncio.run(run())


def test_shadow_deployment_serves_health_panel_and_explains_disabled_approvals(
    tmp_path: Path,
) -> None:
    app = create_operator_app(
        environ={"APP_ENV": "test", "LOCAL_SQLITE_PATH": str(tmp_path / "state.sqlite3")}
    )

    live, ready, panel, queue = call(
        app, "/health/live", "/health/ready", "/", "/api/v1/approvals"
    )

    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert panel.status_code == 200 and "действия закрыты" in panel.text
    codes = {item["code"] for item in queue.json()["blockers"]}
    assert {
        "OPERATOR_AUTH_NOT_CONFIGURED",
        "TRADING_MODE_SHADOW",
        "APPROVAL_SECRET_NOT_CONFIGURED",
    } <= codes


def test_approval_mode_without_durable_storage_refuses_to_wire_the_gateway(
    tmp_path: Path,
) -> None:
    app = create_operator_app(
        environ={
            "APP_ENV": "test",
            "LOCAL_SQLITE_PATH": str(tmp_path / "state.sqlite3"),
            "TRADING_MODE": "APPROVAL",
            "EXECUTOR_URL": "http://executor:3000",
            "EXECUTOR_INTERNAL_TOKEN": "internal-test-value",
            "EXECUTOR_APPROVAL_SECRET": "approval-test-value-0123456789abcdef",
        }
    )

    (queue,) = call(app, "/api/v1/approvals")

    codes = {item["code"] for item in queue.json()["blockers"]}
    assert "DURABLE_EXECUTION_STORE_REQUIRED" in codes
    assert "APPROVAL_SECRET_NOT_CONFIGURED" not in codes
    assert "approval-test-value" not in queue.text
