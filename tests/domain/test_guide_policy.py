"""guide-v1 is the default rule bundle; shadow-hypothesis-v1 stays reproducible (G16)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.worker.policy import (
    GUIDE_RULESET_VERSION,
    LEGACY_RULESET_VERSION,
    default_policy,
    hypothesis_policy,
    load_pipeline_policy,
)


def test_default_bundle_is_guide_v1_with_the_spec_6_6_table() -> None:
    policy = default_policy()
    guide = policy.screening.guide

    assert policy.ruleset_version == GUIDE_RULESET_VERSION == "guide-v1"
    assert guide is not None
    assert guide.minimum_market_cap == Decimal("500000")
    assert guide.preferred_market_cap == Decimal("1000000")
    assert guide.minimum_token_age == timedelta(hours=2)
    assert policy.screening.maximum_holder_concentration == Decimal("0.30")
    assert guide.minimum_volume_per_minute == Decimal("14000")
    assert guide.overvolume_volume_per_minute == Decimal("100000")
    assert guide.minimum_volume_to_liquidity == Decimal("0.5")
    assert guide.minimum_total_fees_sol == Decimal("30")
    assert policy.strategy.guide is not None
    assert policy.features.guide is not None
    assert policy.strategy.guide.spot_minimum_volume_per_minute == guide.minimum_volume_per_minute
    assert policy.strategy.guide.overvolume_volume_per_minute == guide.overvolume_volume_per_minute


def test_shadow_hypothesis_v1_is_reproduced_unchanged() -> None:
    legacy = hypothesis_policy()

    assert legacy.ruleset_version == LEGACY_RULESET_VERSION == "shadow-hypothesis-v1"
    assert legacy.screening.guide is None
    assert legacy.strategy.guide is None
    assert legacy.features.guide is None
    assert legacy.screening.minimum_pool_age == timedelta(days=3)
    assert legacy.strategy.price_shock_fraction == Decimal("0.10")


def test_loader_selects_a_version_and_rejects_unknown_ones() -> None:
    assert load_pipeline_policy({}).ruleset_version == "guide-v1"
    assert (
        load_pipeline_policy({"RULESET_VERSION": "shadow-hypothesis-v1"}).ruleset_version
        == "shadow-hypothesis-v1"
    )
    with pytest.raises(ValueError, match="unknown ruleset"):
        load_pipeline_policy({"RULESET_VERSION": "guide-v0"})


def test_threshold_overrides_require_a_new_config_version(tmp_path: Path) -> None:
    tuned = tmp_path / "tuned.yaml"
    tuned.write_text(
        'config_version: "owner-2026-10-23"\n'
        "thresholds:\n"
        '  minimum_market_cap: "1000000"\n'
        "  minimum_token_age: 10800\n",
        encoding="utf-8",
    )
    unversioned = tmp_path / "unversioned.yaml"
    unversioned.write_text('thresholds:\n  minimum_market_cap: "1000000"\n', encoding="utf-8")
    binary_float = tmp_path / "float.yaml"
    binary_float.write_text(
        'config_version: "owner-2"\nthresholds:\n  minimum_fee_efficiency: 0.6\n',
        encoding="utf-8",
    )

    policy = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(tuned)})

    assert policy.ruleset_version == "guide-v1"
    assert policy.config_version == "owner-2026-10-23"
    assert policy.screening.guide is not None
    assert policy.screening.guide.minimum_market_cap == Decimal("1000000")
    assert policy.screening.guide.minimum_token_age == timedelta(hours=3)
    with pytest.raises(ValueError, match="config_version"):
        load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(unversioned)})
    with pytest.raises(ValueError, match="binary float"):
        load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(binary_float)})
