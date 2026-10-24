"""F3 (C6, C1): the api judges by the worker's policy, and no container log grows unbounded."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.execution.settings import (
    SWAP_AFTER_EXIT,
    SWAP_AFTER_EXIT_ATTEMPTS,
    SWAP_AFTER_EXIT_MAX_SPLITS,
    SWAP_MAX_PRICE_IMPACT_BPS,
    SWAP_SETTLEMENT_TIMEOUT_SECONDS,
    SWAP_SLIPPAGE_BPS,
)
from src.worker.policy import (
    GMGN_REQUIRED_FOR_LIVE_ENV,
    PRIORITY_FEE_MAX_ENV,
    RULESET_OVERRIDES_ENV,
    RULESET_VERSION_ENV,
    UNIVERSE_INTERVAL_ENV,
)
from src.worker.runtime import POSITION_SNAPSHOT_INTERVAL_ENV

# Everything ``load_pipeline_policy(environ)`` reads: thresholds, ruleset, cadence, GMGN rule
# and the priority fee ceiling SHADOW pays (T19).
POLICY_VARIABLES = (
    RULESET_VERSION_ENV,
    RULESET_OVERRIDES_ENV,
    UNIVERSE_INTERVAL_ENV,
    GMGN_REQUIRED_FOR_LIVE_ENV,
    PRIORITY_FEE_MAX_ENV,
)
# The sale after an exit (T17): read by the worker through its runtime (T19).
SWAP_VARIABLES = (
    SWAP_AFTER_EXIT,
    SWAP_MAX_PRICE_IMPACT_BPS,
    SWAP_SLIPPAGE_BPS,
    SWAP_AFTER_EXIT_MAX_SPLITS,
    SWAP_AFTER_EXIT_ATTEMPTS,
    SWAP_SETTLEMENT_TIMEOUT_SECONDS,
)
# The executor's priority fee (executor/src/sdk.ts ``priorityFeePolicyFromEnvironment``).
PRIORITY_FEE_VARIABLES = (
    PRIORITY_FEE_MAX_ENV,
    "PRIORITY_FEE_PERCENTILE",
    "PRIORITY_FEE_MIN_MICRO_LAMPORTS",
)


def env_example_names() -> set[str]:
    return {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    }


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

    assert worker["HISTORY_RETENTION_HOURS"] == "${HISTORY_RETENTION_HOURS:-}"
    assert "HISTORY_RETENTION_HOURS" in env_example_names()


@pytest.mark.parametrize("name", [*SWAP_VARIABLES, POSITION_SNAPSHOT_INTERVAL_ENV])
def test_the_worker_receives_the_sale_settings_and_the_position_snapshot_cadence(
    name: str,
) -> None:
    worker = services()["worker"]["environment"]

    assert worker[name] == f"${{{name}:-}}"
    assert name in env_example_names()


@pytest.mark.parametrize("name", PRIORITY_FEE_VARIABLES)
def test_the_money_executor_receives_the_priority_fee_settings(name: str) -> None:
    executor = services()["executor"]["environment"]

    assert executor[name] == f"${{{name}:-}}"
    assert name in env_example_names()


def test_shadow_and_the_executor_read_one_priority_fee_ceiling() -> None:
    worker = services()["worker"]["environment"]
    executor = services()["executor"]["environment"]

    assert worker[PRIORITY_FEE_MAX_ENV] == executor[PRIORITY_FEE_MAX_ENV]
