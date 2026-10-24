"""Policy settings of the live pipeline: observation gap, bin tolerance, conservative model."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.domain.screening import VolumeIntegrityMode
from src.market.windows import ShortWindowRules
from src.worker.policy import hypothesis_policy, load_pipeline_policy


def test_the_ath_observation_gap_follows_the_configured_universe_interval() -> None:
    default = load_pipeline_policy({})
    fast = load_pipeline_policy({"WORKER_UNIVERSE_INTERVAL_SECONDS": "120"})
    slow = load_pipeline_policy({"WORKER_UNIVERSE_INTERVAL_SECONDS": "900"})

    assert default.features.guide is not None and fast.features.guide is not None
    assert slow.features.guide is not None
    # The cadence plus scheduling slack: max(30 s, a tenth of the interval).
    assert default.features.guide.maximum_observation_gap == timedelta(minutes=5, seconds=30)
    assert fast.features.guide.maximum_observation_gap == timedelta(seconds=150)
    assert slow.features.guide.maximum_observation_gap == timedelta(seconds=990)
    # A window starts no further back than one step; at the default cadence that is the gap.
    assert default.short_window.maximum_interval == timedelta(minutes=5, seconds=30)
    assert slow.short_window.maximum_interval == timedelta(seconds=990)
    # Sweeps 2 minutes apart are summed until they cover 4.5 minutes, so the newest start
    # that old lies up to one more gap back: 270 s + 150 s.
    assert fast.short_window.minimum_interval == timedelta(minutes=4, seconds=30)
    assert fast.short_window.maximum_interval == timedelta(seconds=420)
    assert hypothesis_policy(universe_interval=timedelta(seconds=120)).short_window == (
        fast.short_window
    )


def test_the_default_short_window_rules_are_the_derivation_defaults() -> None:
    assert load_pipeline_policy({}).short_window == ShortWindowRules()
    assert hypothesis_policy().short_window == ShortWindowRules()


def test_the_short_window_tolerance_is_a_versioned_guide_setting(tmp_path: Path) -> None:
    overrides = tmp_path / "tolerance.yaml"
    overrides.write_text(
        'config_version: "owner-6"\nthresholds:\n  short_window_interval_tolerance: 0\n',
        encoding="utf-8",
    )

    default = load_pipeline_policy({})
    strict = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})

    assert default.short_window.interval_tolerance == timedelta(seconds=30)
    assert strict.short_window.interval_tolerance == timedelta(0)
    assert strict.short_window.minimum_interval == timedelta(minutes=5)


def test_the_observation_gap_is_not_a_separate_threshold(tmp_path: Path) -> None:
    overrides = tmp_path / "gap.yaml"
    overrides.write_text(
        'config_version: "owner-3"\nthresholds:\n  maximum_observation_gap: 900\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="maximum_observation_gap"):
        load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})


def test_active_bin_tolerance_and_the_conservative_model_are_policy_settings(
    tmp_path: Path,
) -> None:
    overrides = tmp_path / "tolerance.yaml"
    overrides.write_text(
        'config_version: "owner-4"\n'
        "thresholds:\n"
        "  active_bin_tolerance: 5\n"
        "  max_slippage_bps: 400\n"
        "  conservative_slippage_bps: 400\n"
        '  conservative_depth_fraction: "0.02"\n',
        encoding="utf-8",
    )

    default = load_pipeline_policy({})
    tuned = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})

    assert default.active_bin_tolerance == 3
    assert tuned.active_bin_tolerance == 5
    assert tuned.conservative_costs.slippage_bps == 400
    assert tuned.conservative_depth_fraction == Decimal("0.02")
    # The conservative fill pays the whole tolerance, more than the modeled shadow slippage.
    assert default.conservative_costs.slippage_bps == default.max_slippage_bps
    assert default.conservative_costs.slippage_bps > default.shadow_costs.slippage_bps


def test_bid_only_upside_exit_and_counter_reset_are_versioned_guide_settings(
    tmp_path: Path,
) -> None:
    overrides = tmp_path / "upside.yaml"
    overrides.write_text(
        'config_version: "owner-5"\n'
        "thresholds:\n"
        "  bid_only_upside_grace: 3600\n"
        '  bid_only_upside_exit: "0.10"\n'
        "  short_window_counter_reset_after: 900\n",
        encoding="utf-8",
    )

    default = load_pipeline_policy({})
    tuned = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})

    assert default.strategy.guide is not None and tuned.strategy.guide is not None
    assert default.strategy.guide.bid_only_upside_grace == timedelta(minutes=30)
    assert default.short_window.counter_reset_after == timedelta(minutes=30)
    assert tuned.config_version == "owner-5"
    assert tuned.strategy.guide.bid_only_upside_grace == timedelta(hours=1)
    assert tuned.strategy.guide.bid_only_upside_exit == Decimal("0.10")
    assert tuned.short_window.counter_reset_after == timedelta(minutes=15)


def test_guide_v1_judges_artificial_volume_on_the_signals_live_data_has() -> None:
    assert load_pipeline_policy({}).screening.volume_integrity is (
        VolumeIntegrityMode.AVAILABLE_SIGNALS
    )
    assert hypothesis_policy().screening.volume_integrity is VolumeIntegrityMode.PARTICIPANTS
