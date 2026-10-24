"""F3 (C2): one secret registry for ``redacted()``, the log redactor and ``/settings``; a
traceback is formatted and redacted before it is written, uvicorn's included."""

import io
import json
import logging
from pathlib import Path
from uuid import uuid4

import pytest
import structlog

from src.core.config import AppConfig
from src.core.logging import configure_logging
from tests.api.support import make_panel

# Every secret setting of the project (spec 8.3, interfaces T09/T16), by environment name.
SECRET_NAMES = (
    "DATABASE_URL",
    "APP_OPERATOR_TOKEN",
    "APP_SESSION_SECRET",
    "HELIUS_API_KEY",
    "EXECUTOR_INTERNAL_TOKEN",
    "EXECUTOR_MODE_TOKEN",
    "EXECUTOR_APPROVAL_SECRET",
    "GMGN_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WALLET_KEYPAIR_FILE",
)


def secret_setting(name: str) -> tuple[dict[str, str], str]:
    """An environment with one secret and the value that must never be printed."""

    secret = f"secret-{uuid4().hex}"
    if name == "DATABASE_URL":
        return {name: f"postgresql+asyncpg://user:{secret}@db/x"}, secret
    if name == "SOLANA_RPC_URL":
        return {name: f"https://mainnet.rpc.example.org/?api-key={secret}"}, secret
    return {name: secret}, secret


def logged(environ: dict[str, str], secret: str) -> str:
    stream = io.StringIO()
    configure_logging(AppConfig.load(environ=environ), stream=stream)
    try:
        raise RuntimeError(f"request failed with {secret}")
    except RuntimeError as error:
        # Exactly what uvicorn does after ServerErrorMiddleware re-raises the exception.
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=error)
        logging.getLogger("stdlib-test").exception("handler failed")
        structlog.get_logger("structlog-test").error("job failed", exc_info=True)
    structlog.get_logger("structlog-test").info("field", detail={"nested": [secret]}, url=secret)
    return stream.getvalue()


@pytest.mark.parametrize("name", [*SECRET_NAMES, "SOLANA_RPC_URL"])
def test_a_secret_in_a_traceback_or_an_event_field_never_reaches_the_log(name: str) -> None:
    environ, secret = secret_setting(name)

    output = logged(environ, secret)

    assert secret not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 4
    # The traces are still there, formatted as text and redacted.
    traces = [record["exception"] for record in records if "exception" in record]
    assert len(traces) == 3
    assert all("RuntimeError: request failed with [REDACTED]" in trace for trace in traces)


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_redacted_config_and_the_settings_page_name_every_registry_secret(
    name: str, tmp_path: Path
) -> None:
    environ, secret = secret_setting(name)
    config = AppConfig.load(environ=environ)

    redacted = config.redacted()

    assert secret not in json.dumps(redacted)
    assert "[REDACTED]" in redacted.values()
    panel = make_panel(tmp_path, environ=environ)
    try:
        response = panel.get("/api/v1/config/effective")
        page = panel.get("/settings")
    finally:
        panel.repositories.close()
    names = {item["name"]: item["configured"] for item in response.json()["secrets"]}
    assert set(SECRET_NAMES) <= names.keys()
    assert names[name] is True
    assert secret not in response.text and secret not in page.text
    assert name in page.text
