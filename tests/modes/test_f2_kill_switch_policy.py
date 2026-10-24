"""The policy for open positions chosen at kill-switch activation is durable mode state
(spec 7.3): the worker, the execution service and the approval desk read it from there."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.clock import FrozenClock
from src.modes import (
    InMemoryModeRepository,
    KillSwitchPolicy,
    ModeService,
    ModeState,
    SqliteModeRepository,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
RULES = "rules-v1"


@pytest.mark.parametrize("policy", list(KillSwitchPolicy))
def test_the_chosen_policy_survives_a_restart_and_is_cleared_on_release(
    tmp_path: Path, policy: KillSwitchPolicy
) -> None:
    path = tmp_path / "modes.sqlite3"
    clock = FrozenClock(NOW)
    before = SqliteModeRepository(path).mode_state()

    ModeService(SqliteModeRepository(path), clock, RULES).activate_kill_switch("stop", policy)
    restarted = SqliteModeRepository(path).mode_state()
    ModeService(SqliteModeRepository(path), clock, RULES).release_kill_switch("ok", "operator")
    released = SqliteModeRepository(path).mode_state()

    assert (before.kill_switch_policy, before.active_kill_switch_policy) == (None, None)
    assert restarted.kill_switch_active is True
    assert restarted.kill_switch_policy is policy
    assert restarted.active_kill_switch_policy is policy
    assert released.kill_switch_active is False
    assert (released.kill_switch_policy, released.active_kill_switch_policy) == (None, None)


def test_a_new_activation_replaces_the_policy() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)

    service.activate_kill_switch("hold first", KillSwitchPolicy.HOLD_AND_ALERT)
    service.activate_kill_switch("now leave", KillSwitchPolicy.ORDERLY_EXIT)

    assert repo.mode_state().kill_switch_policy is KillSwitchPolicy.ORDERLY_EXIT


def test_a_state_activated_without_a_recorded_policy_holds() -> None:
    """A state persisted before the policy was stored: the conservative policy applies."""

    legacy = ModeState(kill_switch_active=True)

    assert legacy.active_kill_switch_policy is KillSwitchPolicy.HOLD_AND_ALERT
