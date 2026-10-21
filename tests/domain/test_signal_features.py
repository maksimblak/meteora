from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.domain.features import (
    SignalReadiness,
    VolumeFeatureEngine,
    VolumeFeatureRules,
)
from src.domain.models.primitives import PublicKey
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    WindowMetrics,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("11111111111111111111111111111111"),
    quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
)


def snapshot(
    at: datetime,
    *,
    short: str,
    long: str,
    price: str = "1",
    tvl: str = "1000",
    transactions: int | None = 100,
    participants: int | None = 20,
) -> PoolSnapshot:
    return PoolSnapshot(
        pool=POOL,
        windows={
            "5m": WindowMetrics(
                volume=Decimal(short),
                fees=Decimal("2"),
                transaction_count=transactions,
                unique_participants=participants,
            ),
            "30m": WindowMetrics(volume=Decimal(long), fees=Decimal("20")),
            "1h": WindowMetrics(volume=Decimal(long) * 2, fees=Decimal("40")),
            "4h": WindowMetrics(volume=Decimal(long) * 8, fees=Decimal("160")),
            "24h": WindowMetrics(volume=Decimal(long) * 48, fees=Decimal("960")),
        },
        tvl=Decimal(tvl),
        depth=Decimal("500"),
        price=Decimal(price),
        bin_step=25,
        created_at=NOW - timedelta(days=10),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def rules(*, minimum: int = 6, age: timedelta = timedelta(minutes=10)) -> VolumeFeatureRules:
    return VolumeFeatureRules(
        ruleset_version="rules-1",
        config_version="config-1",
        baseline_points=6,
        minimum_baseline_points=minimum,
        maximum_history_points=20,
        maximum_source_age=age,
    )


def history(*, current_short: str = "400", current_price: str = "1") -> list[PoolSnapshot]:
    prior = [
        snapshot(NOW - timedelta(minutes=35 - index * 5), short="100", long="600")
        for index in range(6)
    ]
    return [*prior, snapshot(NOW, short=current_short, long="1200", price=current_price)]


def test_volume_features_are_golden_and_exclude_current_from_baseline() -> None:
    result = VolumeFeatureEngine().compute(history(), rules(), decision_at=NOW)

    assert result.readiness is SignalReadiness.READY
    assert result.baseline_points == 6
    assert result.short_median == Decimal("100")
    assert result.long_median == Decimal("600")
    assert result.short_ratio == Decimal("4")
    assert result.long_ratio == Decimal("2")
    assert result.robust_z_score == Decimal("10")
    assert result.fee_tvl == Decimal("0.02")
    assert result.ruleset_version == "rules-1"
    assert result.config_version == "config-1"


def test_future_snapshot_is_ignored_without_look_ahead() -> None:
    samples = history()
    samples.append(snapshot(NOW + timedelta(minutes=5), short="999999", long="999999"))

    result = VolumeFeatureEngine().compute(samples, rules(), decision_at=NOW)

    assert result.observed_at == NOW
    assert result.current_short_volume == Decimal("400")
    assert result.short_ratio == Decimal("4")


def test_late_ingested_snapshot_is_not_visible_at_decision_time() -> None:
    samples = history()
    late = snapshot(NOW, short="999999", long="999999").model_copy(
        update={
            "observed_at": NOW - timedelta(minutes=1),
            "source_at": NOW - timedelta(minutes=1),
            "ingested_at": NOW + timedelta(minutes=1),
        }
    )
    samples.insert(-1, late)

    result = VolumeFeatureEngine().compute(samples, rules(), decision_at=NOW)

    assert result.current_short_volume == Decimal("400")


def test_mad_score_is_robust_to_a_single_baseline_outlier() -> None:
    values = ("95", "98", "100", "102", "105", "1000")
    samples = [
        snapshot(NOW - timedelta(minutes=35 - index * 5), short=value, long="600")
        for index, value in enumerate(values)
    ]
    samples.append(snapshot(NOW, short="400", long="1200"))

    result = VolumeFeatureEngine().compute(samples, rules(), decision_at=NOW)

    assert result.short_median == Decimal("101")
    assert result.robust_z_score is not None
    assert result.robust_z_score > Decimal("30")


def test_warming_up_and_stale_are_explicit_not_safe() -> None:
    samples = history()[:4]
    decision_at = samples[-1].observed_at + timedelta(minutes=11)

    result = VolumeFeatureEngine().compute(samples, rules(), decision_at=decision_at)

    assert result.readiness is SignalReadiness.WARMING_UP
    assert result.freshness is Freshness.STALE
    assert result.data_quality is DataQuality.PARTIAL
    assert "baseline_history" in result.missing_signals
    assert "freshness" in result.missing_signals


def test_conflicting_same_time_snapshot_is_rejected() -> None:
    samples = history()
    samples.insert(-1, snapshot(samples[-2].observed_at, short="101", long="600"))

    with pytest.raises(ValueError, match="conflicting snapshots"):
        VolumeFeatureEngine().compute(samples, rules(), decision_at=NOW)


@pytest.mark.parametrize("changed_field", ["base_mint", "quote_mint"])
def test_feature_history_rejects_any_pool_ref_mismatch(changed_field: str) -> None:
    samples = history()
    replacement = POOL.quote_mint if changed_field == "base_mint" else POOL.base_mint
    mismatched_pool = POOL.model_copy(update={changed_field: replacement})
    samples[0] = samples[0].model_copy(update={"pool": mismatched_pool})

    with pytest.raises(ValueError, match="one pool"):
        VolumeFeatureEngine().compute(samples, rules(), decision_at=NOW)


def test_feature_contract_rejects_wall_clock_and_binary_float_inputs() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        VolumeFeatureEngine().compute(history(), rules(), decision_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="binary float"):
        VolumeFeatureRules(
            ruleset_version="r",
            config_version="c",
            baseline_points=6,
            minimum_baseline_points=6,
            maximum_history_points=10,
            zero_dispersion_score=10.0,  # type: ignore[arg-type]
        )
