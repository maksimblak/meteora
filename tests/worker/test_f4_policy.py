"""F4 (spec 6.8, No 49): SHADOW books an estimate of the priority fee per transaction - a
guide-v1 setting - and the executor's ceiling ``PRIORITY_FEE_MAX_LAMPORTS`` only caps it."""

from pathlib import Path

import pytest

from src.worker.policy import (
    LEGACY_RULESET_VERSION,
    PRIORITY_FEE_MAX_ENV,
    RULESET_OVERRIDES_ENV,
    RULESET_VERSION_ENV,
    GuidePolicySettings,
    load_pipeline_policy,
)

CEILING = 15_000_000  # the executor's default ceiling: 0.015 SOL a transaction


def test_shadow_books_the_estimate_below_the_ceiling_not_the_ceiling() -> None:
    default = load_pipeline_policy({})
    legacy = load_pipeline_policy({RULESET_VERSION_ENV: LEGACY_RULESET_VERSION})

    # The build's starting estimate: 1 000 000 lamports (0.001 SOL) a transaction.
    assert GuidePolicySettings().shadow_priority_fee_lamports == 1_000_000
    assert default.shadow_costs.priority_fee_lamports == 1_000_000 < CEILING
    assert default.conservative_costs.priority_fee_lamports == 1_000_000
    assert legacy.shadow_costs.priority_fee_lamports == 1_000_000


@pytest.mark.parametrize(("ceiling", "booked"), [("400000", 400_000), ("5000000", 1_000_000)])
def test_the_ceiling_only_caps_the_estimate(ceiling: str, booked: int) -> None:
    policy = load_pipeline_policy({PRIORITY_FEE_MAX_ENV: ceiling})

    assert policy.shadow_costs.priority_fee_lamports == booked
    assert policy.conservative_costs.priority_fee_lamports == booked


@pytest.mark.parametrize(("ceiling", "booked"), [("", 2_500_000), ("2000000", 2_000_000)])
def test_the_estimate_is_a_versioned_guide_v1_setting(
    tmp_path: Path, ceiling: str, booked: int
) -> None:
    overrides = tmp_path / "ruleset.yaml"
    overrides.write_text(
        "config_version: tuned-priority\nthresholds:\n  shadow_priority_fee_lamports: 2500000\n",
        encoding="utf-8",
    )

    policy = load_pipeline_policy(
        {RULESET_OVERRIDES_ENV: str(overrides), PRIORITY_FEE_MAX_ENV: ceiling}
    )

    assert policy.config_version == "tuned-priority"
    assert policy.shadow_costs.priority_fee_lamports == booked


def test_a_negative_estimate_is_refused() -> None:
    with pytest.raises(ValueError):
        GuidePolicySettings(shadow_priority_fee_lamports=-1)
