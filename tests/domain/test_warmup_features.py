"""D07: derived short windows feed the spike signals; volume history warms up a new pool."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.domain.features import SignalReadiness, VolumeFeatureEngine, VolumeFeatureRules
from src.domain.features.models import GuideFeatureRules
from src.domain.models.primitives import PublicKey
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    ShortWindowDerivation,
    ShortWindowStatus,
    VolumeBucket,
    WindowMetrics,
    WindowOrigin,
)

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"),
    base_mint=PublicKey("So11111111111111111111111111111111111111112"),
    quote_mint=PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
)
FIVE = timedelta(minutes=5)
RULES = VolumeFeatureRules(
    ruleset_version="rules",
    config_version="config",
    baseline_points=12,
    minimum_baseline_points=6,
)


def live(
    at: datetime,
    *,
    short: str | None,
    long: str = "12000",
    price: str = "100",
    hour_fees: str = "50",
    hour_protocol: str | None = "5",
) -> PoolSnapshot:
    windows = {
        "30m": WindowMetrics(volume=Decimal(long), fees=Decimal("6"), protocol_fees=Decimal("1")),
        "1h": WindowMetrics(
            volume=Decimal("100000"),
            fees=Decimal(hour_fees),
            protocol_fees=None if hour_protocol is None else Decimal(hour_protocol),
        ),
        "4h": WindowMetrics(volume=Decimal("400000"), fees=Decimal("200")),
        "24h": WindowMetrics(volume=Decimal("2400000"), fees=Decimal("1200")),
    }
    derivation = ShortWindowDerivation(
        window="5m",
        origin=WindowOrigin.CUMULATIVE_DELTA,
        status=ShortWindowStatus.UNKNOWN,
        reason="NO_PREVIOUS",
    )
    if short is not None:
        windows["5m"] = WindowMetrics(volume=Decimal(short), fees=Decimal(short) / 1000)
        derivation = ShortWindowDerivation(
            window="5m",
            origin=WindowOrigin.CUMULATIVE_DELTA,
            status=ShortWindowStatus.DERIVED,
            previous_source_at=at - FIVE,
            interval_seconds=Decimal(300),
        )
    return PoolSnapshot(
        pool=POOL,
        windows=windows,
        tvl=Decimal("500000"),
        depth=None,
        price=Decimal(price),
        bin_step=4,
        created_at=NOW - timedelta(days=30),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        base_fee_rate=Decimal("0.0005"),
        short_window=derivation,
    )


def buckets(
    end: datetime, count: int, volume: str = "1000", *, pool: PoolRef = POOL
) -> list[VolumeBucket]:
    """``count`` contiguous 5m buckets, the last one ending at ``end``."""

    return [
        VolumeBucket(
            pool_address=pool.address,
            start=end - FIVE * (count - index),
            duration=FIVE,
            volume=Decimal(volume),
        )
        for index in range(count)
    ]


def test_a_new_pool_is_ready_from_its_volume_history_without_waiting_for_snapshots() -> None:
    history = [live(NOW - FIVE, short=None), live(NOW, short="6000")]
    # The current 5m window covers NOW-5m..NOW; the baseline ends where it starts.
    warmup = buckets(NOW - FIVE, 17)

    features = VolumeFeatureEngine().compute(history, RULES, decision_at=NOW, warmup=warmup)

    assert features.readiness is SignalReadiness.READY
    assert features.baseline_points == 12
    assert features.short_median == Decimal("1000")
    assert features.short_ratio == Decimal("6")
    assert features.robust_z_score == Decimal("10")  # zero dispersion, upwards
    assert features.long_median == Decimal("6000")  # six 1000 buckets per 30 minutes
    assert features.long_ratio == Decimal("2")
    assert features.short_window_origin is WindowOrigin.CUMULATIVE_DELTA
    assert features.baseline_origin is WindowOrigin.VOLUME_HISTORY


def test_volume_history_never_reaches_past_the_decision_or_into_the_current_window() -> None:
    history = [live(NOW - FIVE, short=None), live(NOW, short="6000")]
    # Two late buckets overlap the current window or end after the decision.
    future = [
        VolumeBucket(pool_address=POOL.address, start=NOW - FIVE, duration=FIVE,
                     volume=Decimal("999999")),
        VolumeBucket(pool_address=POOL.address, start=NOW, duration=FIVE,
                     volume=Decimal("999999")),
    ]

    features = VolumeFeatureEngine().compute(
        history, RULES, decision_at=NOW, warmup=[*buckets(NOW - FIVE, 17), *future]
    )

    assert features.short_median == Decimal("1000")
    assert features.long_median == Decimal("6000")


def test_volume_history_of_another_pool_is_refused() -> None:
    other = PoolRef(
        address=PublicKey("SysvarRent111111111111111111111111111111111"),
        base_mint=POOL.base_mint,
        quote_mint=POOL.quote_mint,
    )

    with pytest.raises(ValueError, match="one pool"):
        VolumeFeatureEngine().compute(
            [live(NOW, short="6000")],
            RULES,
            decision_at=NOW,
            warmup=buckets(NOW - FIVE, 17, pool=other),
        )


def test_own_derived_windows_are_the_baseline_once_there_are_enough() -> None:
    calm = [live(NOW - FIVE * (8 - index), short="2000") for index in range(8)]
    history = [*calm, live(NOW, short="6000")]

    features = VolumeFeatureEngine().compute(
        history, RULES, decision_at=NOW, warmup=buckets(NOW - FIVE, 17)
    )

    assert features.readiness is SignalReadiness.READY
    assert features.short_median == Decimal("2000")  # snapshots, not the 1000 buckets
    assert features.short_ratio == Decimal("3")
    assert features.baseline_origin is WindowOrigin.CUMULATIVE_DELTA


def test_without_a_short_window_the_last_closed_bucket_is_the_current_one() -> None:
    history = [live(NOW - FIVE, short=None), live(NOW, short=None)]
    warmup = [*buckets(NOW - FIVE * 2, 17), *buckets(NOW - FIVE, 1, volume="4000")]

    features = VolumeFeatureEngine().compute(history, RULES, decision_at=NOW, warmup=warmup)

    assert features.current_short_volume == Decimal("4000")
    assert features.short_window_origin is WindowOrigin.VOLUME_HISTORY
    assert features.short_median == Decimal("1000")
    assert features.short_ratio == Decimal("4")


def test_organicity_counts_the_protocol_share_of_the_fees() -> None:
    guide = RULES.model_copy(update={"guide": GuideFeatureRules()})
    history = [live(NOW - FIVE, short="1000"), live(NOW, short="6000")]

    features = VolumeFeatureEngine().compute(history, guide, decision_at=NOW)
    lp_only = VolumeFeatureEngine().compute(
        [live(NOW - FIVE, short="1000"), live(NOW, short="6000", hour_protocol=None)],
        guide,
        decision_at=NOW,
    )

    # (50 LP + 5 protocol) / (100 000 * 0.0005) = 55 / 50
    assert features.fee_efficiency == Decimal("1.1")
    assert lp_only.fee_efficiency == Decimal("1")


def test_identical_bursts_in_a_row_are_measured_as_self_similar() -> None:
    steady = [live(NOW - FIVE * (3 - index), short=value)
              for index, value in enumerate(("500", "5000", "5050"))]
    history = [*steady, live(NOW, short="5025")]
    organic = [*steady[:2], live(NOW - FIVE, short="2000"), live(NOW, short="5025")]

    similar = VolumeFeatureEngine().compute(history, RULES, decision_at=NOW)
    varied = VolumeFeatureEngine().compute(organic, RULES, decision_at=NOW)

    # The last three short windows 5000, 5050, 5025: spread (5050 - 5000) / 5050.
    assert similar.short_burst_spread == Decimal(50) / Decimal(5050)
    assert varied.short_burst_spread == Decimal(3025) / Decimal(5025)
