"""guide-v1 feature inputs: per-minute volume, turnover, fee organicity and price highs."""

from datetime import timedelta
from decimal import Decimal

from src.domain.features import VolumeFeatureEngine, VolumeFeatureRules
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_feature_rules,
    guide_features,
    guide_history,
    guide_snapshot,
)


def test_savash_case_turnover_and_fee_organicity_from_current_windows() -> None:
    """Video 2: ~980k hourly volume on 87k liquidity, fees far below the 1% base fee."""

    history = guide_history(
        short="81000", hour="980000", hour_fees="900", tvl="87000", base_fee_rate="0.01"
    )

    features = guide_features(history)

    assert features.volume_per_minute == Decimal("16200")
    assert features.volume_to_liquidity == Decimal("81000") / Decimal("87000")
    # 900 collected / (980 000 * 1%) expected = 0.0918...
    assert features.fee_efficiency == Decimal("900") / Decimal("9800")


def test_fee_organicity_is_unknown_without_base_fee_or_volume() -> None:
    no_fee_rate = guide_features(guide_history(base_fee_rate=None))
    no_volume = guide_features(guide_history(hour="0", hour_fees="0"))

    assert no_fee_rate.fee_efficiency is None
    assert no_volume.fee_efficiency is None
    assert no_fee_rate.volume_per_minute == Decimal("20000")


def test_local_high_uses_only_the_lookback_window_and_no_future_snapshot() -> None:
    prices = ("3", "1", "1.5", "1.1", "1", "1", "1.2")  # 30 minutes ago: 3, then 1.5 peak
    history = guide_history(prices=prices)
    future = guide_snapshot(NOW + timedelta(minutes=5), price="9")

    features = guide_features([*history, future], local_high_lookback=timedelta(minutes=25))

    assert features.local_high_price == Decimal("1.5")
    assert features.observed_at == NOW


def test_all_time_high_is_known_only_when_history_covers_the_pool_life() -> None:
    prices = ("1", "1.4", "1.3", "1.2", "1.1", "1.3", "1.6")
    covered = guide_history(prices=prices, created_at=NOW - timedelta(minutes=33))
    uncovered = guide_history(prices=prices, created_at=NOW - timedelta(hours=6))

    known = guide_features(covered, ath_coverage_tolerance=timedelta(minutes=5))
    unknown = guide_features(uncovered, ath_coverage_tolerance=timedelta(minutes=5))

    assert known.all_time_high_price == Decimal("1.4")  # prior history only, current excluded
    assert unknown.all_time_high_price is None


def test_all_time_high_is_unknown_when_the_history_has_a_gap() -> None:
    """Snapshot 10 minutes after creation, then 2h15m unobserved: a peak may hide there."""

    created = NOW - timedelta(hours=3)
    first = guide_snapshot(
        created + timedelta(minutes=10), short="20000", long="120000", created_at=created
    )
    recent = guide_history(
        prices=("1", "1.1", "1.2", "1.1", "1.2", "1.25", "1.3"), created_at=created
    )

    features = guide_features([first, *recent])

    assert features.all_time_high_price is None


def test_volume_to_liquidity_is_always_per_five_minutes_whatever_the_window() -> None:
    history = guide_history(long="600000", tvl="250000")

    five_minutes = guide_features(history)
    thirty_minutes = guide_features(history, volume_window="30m")

    # 30m: 600 000 / 30 = 20 000 a minute -> 100 000 per 5 minutes on 250 000 = 0.4, not 2.4
    assert thirty_minutes.volume_per_minute == Decimal("20000")
    assert thirty_minutes.volume_to_liquidity == Decimal("0.4")
    assert five_minutes.volume_to_liquidity == Decimal("100000") / Decimal("250000")


def test_legacy_rules_leave_guide_features_empty_and_quality_unchanged() -> None:
    history = guide_history(base_fee_rate=None, market_cap=None)
    legacy_rules = VolumeFeatureRules(
        ruleset_version=RULESET,
        config_version=CONFIG,
        baseline_points=6,
        minimum_baseline_points=6,
        maximum_history_points=100,
    )

    legacy = VolumeFeatureEngine().compute(history, legacy_rules, decision_at=NOW)
    guided = guide_features(history)

    assert legacy.volume_per_minute is None
    assert legacy.fee_efficiency is None
    assert legacy.local_high_price is None
    assert legacy.missing_signals == guided.missing_signals == ()
    assert legacy.data_quality is guided.data_quality


def test_a_history_older_than_the_lookback_has_no_local_high_and_is_stale() -> None:
    # A restart on yesterday's universe: the newest snapshot is two hours old (it crashed the
    # cycle with "max() iterable argument is empty" before).
    history = guide_history()

    features = VolumeFeatureEngine().compute(
        history, guide_feature_rules(), decision_at=NOW + timedelta(hours=2)
    )

    assert features.local_high_price is None
    assert features.freshness.value == "STALE"
