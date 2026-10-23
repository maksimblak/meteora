"""Compose hands operator auth from ``.env`` to the api by name only (spec 5.6, 8.3).

Compose resolves ``${NAME:-}`` from ``.env``; an unset name becomes an empty string,
which the configuration treats as "not set". So a ``*_FILE``-only setup never puts a
raw value into the api environment, and an empty ``.env`` still starts the stack.
"""

import re
from pathlib import Path

import yaml

from src.api.settings import OperatorSettings
from src.core.config import AppConfig

PASS_THROUGH = re.compile(r"\$\{([A-Z0-9_]+):-\}")
OPERATOR_AUTH = (
    "APP_OPERATOR_TOKEN",
    "APP_OPERATOR_TOKEN_FILE",
    "APP_SESSION_SECRET",
    "APP_SESSION_SECRET_FILE",
)


def api_environment() -> dict[str, str]:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    return dict(compose["services"]["api"]["environment"])


def resolved(environment: dict[str, str], dotenv: dict[str, str]) -> dict[str, str]:
    """What compose puts into the container for pass-through entries."""

    values = {}
    for name, value in environment.items():
        match = PASS_THROUGH.fullmatch(str(value))
        values[name] = dotenv.get(match.group(1), "") if match else str(value)
    return values


def test_operator_login_names_reach_the_api_without_values_in_compose() -> None:
    environment = api_environment()

    for name in OPERATOR_AUTH:
        assert environment[name] == f"${{{name}:-}}", name
    for name, value in environment.items():
        if any(part in name for part in ("TOKEN", "SECRET")):
            assert PASS_THROUGH.fullmatch(str(value)), f"{name} must be a pass-through"


def test_a_file_only_setup_enables_login_and_keeps_raw_values_out_of_env(
    tmp_path: Path,
) -> None:
    token = tmp_path / "operator-token"
    session = tmp_path / "session-secret"
    token.write_text("file-token-test-value\n", encoding="utf-8")
    session.write_text("file-session-test-value\n", encoding="utf-8")

    container = resolved(
        api_environment(),
        {"APP_OPERATOR_TOKEN_FILE": str(token), "APP_SESSION_SECRET_FILE": str(session)},
    )
    config = AppConfig.load(environ=container)

    assert container["APP_OPERATOR_TOKEN"] == "" and container["APP_SESSION_SECRET"] == ""
    assert "file-token-test-value" not in "".join(container.values())
    assert OperatorSettings.from_environment(config, container).auth_configured is True


def test_an_empty_dotenv_still_starts_with_login_reported_as_off() -> None:
    container = resolved(api_environment(), {})
    config = AppConfig.load(environ=container)

    assert OperatorSettings.from_environment(config, container).auth_configured is False
