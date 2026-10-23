"""The worker takes its rule bundle from configuration in one place: ``build_runtime``."""

from pathlib import Path

import pytest

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.worker.runtime import build_runtime
from tests.worker.harness import NOW


def started_ruleset(tmp_path: Path, **variables: str) -> str:
    environ = {
        "APP_ENV": "development",
        "LOCAL_SQLITE_PATH": str(tmp_path / "worker.sqlite3"),
        **variables,
    }
    runtime, _ = build_runtime(AppConfig.load(environ=environ), environ, clock=FrozenClock(NOW))
    try:
        return runtime.policy.ruleset_version
    finally:
        runtime.close()


def test_without_configuration_the_worker_applies_the_guide_rules(tmp_path: Path) -> None:
    assert started_ruleset(tmp_path) == "guide-v1"


def test_the_previous_ruleset_is_still_selectable(tmp_path: Path) -> None:
    assert started_ruleset(tmp_path, RULESET_VERSION="shadow-hypothesis-v1") == (
        "shadow-hypothesis-v1"
    )


def test_an_unknown_ruleset_stops_the_start_before_any_store_is_opened(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown ruleset version"):
        started_ruleset(tmp_path, RULESET_VERSION="guide-v2")

    assert not (tmp_path / "worker.sqlite3").exists()
