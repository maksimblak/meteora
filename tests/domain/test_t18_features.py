"""T18: the pool's own hourly volume per minute and hourly turnover (G24 inputs)."""

from decimal import Decimal

from src.domain.features import VolumeFeatureEngine, VolumeFeatureRules
from src.market.models import DataQuality
from tests.domain.guide_fixtures import CONFIG, NOW, RULESET, guide_features, guide_history


def test_the_hour_window_gives_volume_per_minute_and_turnover_against_the_tvl() -> None:
    # Savash, video 2: ~980 000 an hour on 87 000 of liquidity.
    features = guide_features(guide_history(hour="980000", tvl="87000"))

    assert features.hourly_volume_per_minute == Decimal("980000") / Decimal("60")
    assert features.hourly_turnover == Decimal("980000") / Decimal("87000")


def test_without_the_hour_or_a_tvl_the_hourly_inputs_are_unknown() -> None:
    history = guide_history(tvl="0")
    data = history[-1].model_dump()
    del data["windows"]["1h"]
    data["data_quality"] = DataQuality.PARTIAL
    no_hour = [*history[:-1], type(history[-1]).model_validate(data)]

    assert guide_features(no_hour).hourly_volume_per_minute is None
    assert guide_features(no_hour).hourly_turnover is None
    assert guide_features(history).hourly_turnover is None  # zero TVL


def test_the_legacy_ruleset_computes_no_hourly_inputs() -> None:
    legacy = VolumeFeatureRules(
        ruleset_version=RULESET,
        config_version=CONFIG,
        baseline_points=6,
        minimum_baseline_points=6,
        maximum_history_points=100,
    )

    features = VolumeFeatureEngine().compute(guide_history(), legacy, decision_at=NOW)

    assert features.hourly_volume_per_minute is None and features.hourly_turnover is None
