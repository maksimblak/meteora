"""F3 (C6, C1): the api judges by the worker's policy, and no container log grows unbounded."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.worker.policy import (
    GMGN_REQUIRED_FOR_LIVE_ENV,
    RULESET_OVERRIDES_ENV,
    RULESET_VERSION_ENV,
    UNIVERSE_INTERVAL_ENV,
)

# Everything ``load_pipeline_policy(environ)`` reads: thresholds, ruleset, cadence, GMGN rule.
POLICY_VARIABLES = (
    RULESET_VERSION_ENV,
    RULESET_OVERRIDES_ENV,
    UNIVERSE_INTERVAL_ENV,
    GMGN_REQUIRED_FOR_LIVE_ENV,
)


def services() -> dict[str, Any]:
    return dict(yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))["services"])


@pytest.mark.parametrize("name", POLICY_VARIABLES)
def test_api_and_worker_receive_the_same_policy_variable(name: str) -> None:
    api = services()["api"]["environment"]
    worker = services()["worker"]["environment"]

    assert name in api and name in worker
    assert api[name] == worker[name] == f"${{{name}:-}}"


def test_every_container_log_is_rotated() -> None:
    for name, service in services().items():
        logging = service.get("logging")
        assert logging is not None, name
        assert logging["driver"] == "json-file", name
        assert logging["options"]["max-size"], name
        assert int(logging["options"]["max-file"]) >= 1, name


def test_the_worker_receives_the_history_retention_window() -> None:
    worker = services()["worker"]["environment"]
    names = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    }

    assert worker["HISTORY_RETENTION_HOURS"] == "${HISTORY_RETENTION_HOURS:-}"
    assert "HISTORY_RETENTION_HOURS" in names
