"""Deployment of the ``gmgn`` service (G18): opt-in profile, internal port, own lock file."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

SERVICE = Path("services/gmgn")
# The check image carries compose and .env.example but not the service sources; these
# guards run from the repository (``uv run pytest``) and in the service's own checks.
needs_service_sources = pytest.mark.skipif(
    not (SERVICE / "pyproject.toml").is_file() or not Path("Dockerfile.gmgn").is_file(),
    reason="services/gmgn and Dockerfile.gmgn are not part of the check image",
)


def compose() -> dict[str, dict[str, object]]:
    document = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    return dict(document["services"])


def test_the_browser_service_is_off_by_default_and_internal_only() -> None:
    gmgn = compose()["gmgn"]

    assert gmgn["profiles"] == ["gmgn"]
    assert gmgn["build"] == {"context": ".", "dockerfile": "Dockerfile.gmgn", "target": "runtime"}
    assert "ports" not in gmgn
    assert gmgn["expose"] == ["8080"]
    assert gmgn["healthcheck"]


def test_the_worker_asks_gmgn_only_when_configured_and_live_needs_it_by_default() -> None:
    worker = compose()["worker"]["environment"]
    api = compose()["api"]["environment"]

    assert isinstance(worker, dict) and isinstance(api, dict)
    assert worker["GMGN_URL"] == "${GMGN_URL:-}"
    assert worker["GMGN_REQUIRED_FOR_LIVE"] == "${GMGN_REQUIRED_FOR_LIVE:-}"
    assert api["GMGN_REQUIRED_FOR_LIVE"] == "${GMGN_REQUIRED_FOR_LIVE:-}"


@needs_service_sources
def test_the_service_has_its_own_lock_and_the_main_lock_has_no_browser() -> None:
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text(encoding="utf-8"))
    names = {
        re.split(r"[<>=!~\[ ]", item, maxsplit=1)[0] for item in project["project"]["dependencies"]
    }
    main_lock = Path("uv.lock").read_text(encoding="utf-8")
    main_project = Path("pyproject.toml").read_text(encoding="utf-8")

    # Official Playwright and the main project's HTTP stack - no stealth or solver package.
    assert names == {"fastapi", "playwright", "pydantic", "uvicorn"}
    assert (SERVICE / "uv.lock").is_file()
    assert 'name = "playwright"' in (SERVICE / "uv.lock").read_text(encoding="utf-8")
    assert "playwright" not in main_lock and "playwright" not in main_project


@needs_service_sources
def test_the_image_is_the_pinned_official_playwright_image_of_the_locked_version() -> None:
    dockerfile = Path("Dockerfile.gmgn").read_text(encoding="utf-8")
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text(encoding="utf-8"))
    (pin,) = [item for item in project["project"]["dependencies"] if item.startswith("playwright")]
    version = pin.split("==")[1]

    (image,) = re.findall(r"^FROM (\S+)", dockerfile, flags=re.MULTILINE)[:1]
    assert image == f"mcr.microsoft.com/playwright/python:v{version}-noble"
    assert "USER pwuser" in dockerfile


def test_example_environment_names_the_gmgn_settings_without_values() -> None:
    lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
    names = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines if "=" in line}

    for name in (
        "GMGN_URL",
        "GMGN_REQUIRED_FOR_LIVE",
        "GMGN_TIMEOUT_SECONDS",
        "GMGN_BRIDGE_TOKEN",
        "GMGN_BRIDGE_TOKEN_FILE",
        "GMGN_BRIDGE_HOST",
        "GMGN_BRIDGE_PORT",
        "GMGN_BRIDGE_PROFILE_DIR",
        "GMGN_BRIDGE_REQUESTS_PER_MINUTE",
    ):
        assert names[name] == "", name


def test_the_worker_reaches_the_bridge_on_the_host_with_the_shared_token() -> None:
    """G25: the bridge runs on the host, not in compose; the worker alone gets its token."""

    services = compose()
    worker = services["worker"]
    environment = worker["environment"]

    assert isinstance(environment, dict)
    for name in ("GMGN_BRIDGE_TOKEN", "GMGN_BRIDGE_TOKEN_FILE"):
        assert environment[name] == "${" + name + ":-}"
    # Docker Desktop knows host.docker.internal; Docker Engine on Linux needs the mapping.
    assert "host.docker.internal:host-gateway" in worker["extra_hosts"]
    for name, service in services.items():
        if name == "worker":
            continue
        variables = service.get("environment") or {}
        assert "GMGN_BRIDGE_TOKEN" not in variables, name


needs_scripts = pytest.mark.skipif(
    not Path("scripts/gmgn-bridge.ps1").is_file(),
    reason="the operator scripts are not part of the check image",
)


@needs_scripts
@pytest.mark.parametrize("script", ["scripts/gmgn-bridge.ps1", "scripts/gmgn-bridge.sh"])
def test_one_command_starts_the_bridge_without_printing_the_token(script: str) -> None:
    text = Path(script).read_text(encoding="utf-8")

    assert "gmgn_service.bridge" in text
    assert "--frozen" in text and "services" in text
    assert "GMGN_BRIDGE_TOKEN" in text
    # The token is read into the process environment only; it is never echoed.
    printing = ("write-host", "write-output", "echo ", "printf")
    for line in text.splitlines():
        lowered = line.lower()
        if any(word in lowered for word in printing):
            assert "token" not in lowered, line


@needs_service_sources
def test_the_browser_code_uses_playwright_defaults_and_solves_nothing() -> None:
    """Spec 6.7 boundaries, as a guard on the service source."""

    source = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in (SERVICE / "gmgn_service").glob("*.py")
    )

    assert "chromium.launch()" in source
    assert "new_context()" in source
    for forbidden in (
        "stealth",
        "user_agent=",
        "add_init_script",
        "storage_state",
        "solver",
        "2captcha",
    ):
        assert forbidden not in source, forbidden
