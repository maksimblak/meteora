"""D07: the short window is the increase of the cumulative counters between adjacent snapshots."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.domain.models.primitives import PublicKey
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    ShortWindowStatus,
    WindowMetrics,
    WindowOrigin,
)
from src.market.windows import ShortWindowRules, derive_short_window

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"),
    base_mint=PublicKey("So11111111111111111111111111111111111111112"),
    quote_mint=PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
)
OTHER = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("So11111111111111111111111111111111111111112"),
    quote_mint=PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
)
RULES = ShortWindowRules()


def live(
    at: datetime,
    volume: str | None,
    fees: str | None,
    *,
    pool: PoolRef = POOL,
    five_minutes: str | None = None,
) -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal("1000"), fees=Decimal("1"))
        for name in ("30m", "1h", "4h", "24h")
    }
    if five_minutes is not None:
        windows["5m"] = WindowMetrics(volume=Decimal(five_minutes), fees=Decimal("1"))
    return PoolSnapshot(
        pool=pool,
        windows=windows,
        tvl=Decimal("100000"),
        depth=None,
        price=Decimal("100"),
        bin_step=4,
        created_at=T0 - timedelta(days=30),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        cumulative_volume=None if volume is None else Decimal(volume),
        cumulative_fees=None if fees is None else Decimal(fees),
    )


def test_the_short_window_is_the_counter_increase_since_the_previous_snapshot() -> None:
    previous = live(T0, "1000000", "2500")
    current = live(T0 + timedelta(minutes=5), "1060000", "2650")

    derived = derive_short_window(current, previous, RULES)

    assert derived.windows["5m"].volume == Decimal("60000")
    assert derived.windows["5m"].fees == Decimal("150")
    assert derived.short_window is not None
    assert derived.short_window.status is ShortWindowStatus.DERIVED
    assert derived.short_window.origin is WindowOrigin.CUMULATIVE_DELTA
    assert derived.short_window.interval_seconds == Decimal("300")
    assert derived.short_window.previous_source_at == T0
    assert derived.short_window.zeroed_noise is False
    # Everything else is the snapshot as the source reported it.
    assert derived.windows["30m"] == current.windows["30m"]
    assert derived.cumulative_volume == Decimal("1060000")


def test_a_delta_over_less_than_the_window_is_never_stretched_to_it() -> None:
    """Review probe C096: after a restart the sweep runs a minute after the last one. A
    20 000 trade in that minute is not a 100 000 window; the next sweep measures five
    minutes from that reading."""

    series = chain(
        RULES,
        (0, "1000000", "100"),
        (1, "1020000", "102"),
        (6, "1050000", "105"),
    )

    assert reasons(series) == ["NO_PREVIOUS", "INTERVAL_TOO_SHORT", None]
    assert "5m" not in series[1].windows
    assert all(
        item.windows["5m"].volume != Decimal("100000") for item in series if "5m" in item.windows
    )
    # 1 050 000 - 1 020 000 over the five minutes since the reading at minute 1.
    assert series[2].windows["5m"].volume == Decimal("30000")
    assert series[2].windows["5m"].fees == Decimal("3")
    assert series[2].short_window is not None
    assert series[2].short_window.interval_seconds == Decimal("300")


def test_the_shortest_interval_that_closes_a_window_is_the_window_less_the_tolerance() -> None:
    assert RULES.interval_tolerance == timedelta(seconds=30)
    assert RULES.minimum_interval == timedelta(minutes=4, seconds=30)
    strict = ShortWindowRules(interval_tolerance=timedelta(0))
    assert strict.minimum_interval == timedelta(minutes=5)
    with pytest.raises(ValueError):
        ShortWindowRules(interval_tolerance=timedelta(minutes=5))
    with pytest.raises(ValueError):
        ShortWindowRules(maximum_interval=timedelta(minutes=4))

    previous = live(T0, "1000000", "2500")
    just_short = derive_short_window(
        live(T0 + timedelta(minutes=4, seconds=29), "1030000", "2575"), previous, RULES
    )
    closing = derive_short_window(
        live(T0 + timedelta(minutes=4, seconds=30), "1030000", "2575"), previous, RULES
    )

    assert reasons([just_short]) == ["INTERVAL_TOO_SHORT"]
    # 30 000 over 270 s is 33 333.3... per 300 s: a slightly short interval is rescaled.
    assert closing.windows["5m"].volume == Decimal(30_000) * Decimal(300) / Decimal(270)


def test_a_fast_cadence_sums_adjacent_deltas_until_they_cover_the_window() -> None:
    """Sweeps 90 s apart: the window starts at the newest reading at least 4.5 min old."""

    rules = ShortWindowRules(maximum_interval=timedelta(minutes=6, seconds=30))
    readings = [(0, "1000000"), (90, "1010000"), (180, "1020000"), (269, "1030000"),
                (360, "1040000"), (450, "1050000")]
    derived: list[PoolSnapshot] = []
    for second, volume in readings:
        current = live(T0 + timedelta(seconds=second), volume, "2500")
        derived.append(derive_short_window(current, derived[-1] if derived else None, rules))

    assert reasons(derived)[:4] == [
        "NO_PREVIOUS", "INTERVAL_TOO_SHORT", "INTERVAL_TOO_SHORT", "INTERVAL_TOO_SHORT"
    ]
    # At 360 s the newest reading 270 s or older is the one at 90 s: 30 000 in 270 s.
    assert derived[4].windows["5m"].volume == Decimal(30_000) * Decimal(300) / Decimal(270)
    # At 450 s it is the reading at 180 s: 30 000 in 270 s again, never a 90 s delta x 3.3.
    assert derived[5].windows["5m"].volume == Decimal(30_000) * Decimal(300) / Decimal(270)
    assert derived[5].short_window is not None
    assert derived[5].short_window.interval_seconds == Decimal("270")


def test_a_small_negative_difference_is_noise_and_counts_as_zero() -> None:
    # Recorded live: the SOL-USDC counter moved by -1.1 within seconds.
    previous = live(T0, "26439418383.468887", "11573615.751864264")
    current = live(T0 + timedelta(minutes=5), "26439418382.368887", "11573615.751864264")

    derived = derive_short_window(current, previous, RULES)

    assert derived.windows["5m"].volume == Decimal("0")
    assert derived.windows["5m"].fees == Decimal("0")
    assert derived.short_window is not None
    assert derived.short_window.status is ShortWindowStatus.DERIVED
    assert derived.short_window.zeroed_noise is True


@pytest.mark.parametrize(
    ("previous", "current", "reason"),
    [
        (live(T0, "1000000", "2500"), live(T0 + timedelta(minutes=5), "995000", "2600"),
         "NEGATIVE_DELTA"),
        (live(T0, "1000000", "2500"), live(T0 + timedelta(minutes=5), "1060000", "2400"),
         "NEGATIVE_DELTA"),
        (live(T0, "1000000", "2500"), live(T0 + timedelta(seconds=30), "1010000", "2510"),
         "INTERVAL_TOO_SHORT"),
        (live(T0, "1000000", "2500"), live(T0 + timedelta(minutes=6), "1060000", "2650"),
         "INTERVAL_TOO_LONG"),
        (None, live(T0, "1000000", "2500"), "NO_PREVIOUS"),
        (live(T0, "1000000", "2500", pool=OTHER), live(T0 + timedelta(minutes=5), "1", "1"),
         "NO_PREVIOUS"),
        (live(T0, None, "2500"), live(T0 + timedelta(minutes=5), "1060000", "2650"),
         "COUNTER_MISSING"),
        (live(T0, "1000000", "2500"), live(T0 + timedelta(minutes=5), "1060000", None),
         "COUNTER_MISSING"),
        # Look-ahead: a snapshot newer than the current one is never its predecessor.
        (live(T0 + timedelta(minutes=5), "1060000", "2650"), live(T0, "1000000", "2500"),
         "INTERVAL_TOO_SHORT"),
    ],
    ids=[
        "volume-drop", "fee-drop", "too-short", "too-long", "first", "other-pool",
        "no-previous-counter", "no-current-counter", "future-previous",
    ],
)
def test_an_unusable_difference_leaves_the_short_window_unknown(
    previous: PoolSnapshot | None, current: PoolSnapshot, reason: str
) -> None:
    derived = derive_short_window(current, previous, RULES)

    assert "5m" not in derived.windows
    assert derived.short_window is not None
    assert derived.short_window.status is ShortWindowStatus.UNKNOWN
    assert derived.short_window.reason == reason
    assert derived.short_window.origin is WindowOrigin.CUMULATIVE_DELTA


def chain(rules: ShortWindowRules, *readings: tuple[int, str, str]) -> list[PoolSnapshot]:
    """Snapshots ``(minute, volume counter, fees counter)``, each derived from the last."""

    derived: list[PoolSnapshot] = []
    for minute, volume, fees in readings:
        current = live(T0 + timedelta(minutes=minute), volume, fees)
        derived.append(derive_short_window(current, derived[-1] if derived else None, rules))
    return derived


def reasons(snapshots: list[PoolSnapshot]) -> list[str | None]:
    return [None if item.short_window is None else item.short_window.reason for item in snapshots]


def test_after_a_counter_drop_the_increase_counts_from_the_mark_and_its_own_time() -> None:
    """Review probes: 1M@t0 -> 900k@t5 -> 1.01M@t10 is 10 000 over 10 minutes (too long for
    one 5m window), the mark moves there, and 1.02M@t15 is 10 000 in five minutes."""

    series = chain(
        RULES,
        (0, "1000000", "2500"),
        (5, "900000", "2250"),
        (10, "1010000", "2525"),
        (15, "1020000", "2550"),
    )

    assert reasons(series) == ["NO_PREVIOUS", "NEGATIVE_DELTA", "INTERVAL_TOO_LONG", None]
    assert "5m" not in series[2].windows
    assert series[3].windows["5m"].volume == Decimal("10000")
    assert series[3].windows["5m"].fees == Decimal("25")
    mark = series[3].short_window.volume_mark if series[3].short_window else None
    assert mark is not None and (mark.value, mark.at) == (
        Decimal("1020000"),
        T0 + timedelta(minutes=15),
    )


def test_an_increase_accumulated_while_below_the_mark_is_never_one_window() -> None:
    # 1M@t0, 900k@t5..t15, 1.008M@t20: 8 000 over 20 minutes is not a 5m spike.
    series = chain(
        RULES,
        (0, "1000000", "2500"),
        *((minute, "900000", "2250") for minute in (5, 10, 15)),
        (20, "1008000", "2520"),
    )

    assert reasons(series)[-1] == "INTERVAL_TOO_LONG"
    assert "5m" not in series[-1].windows


def test_a_counter_that_stays_low_and_grows_is_a_reset_after_the_configured_time() -> None:
    rules = ShortWindowRules(counter_reset_after=timedelta(minutes=15))
    series = chain(
        rules,
        (0, "1000000", "2500"),
        (5, "0", "0"),
        (10, "1000", "2.5"),
        (15, "2000", "5"),
        (20, "3000", "7.5"),
        (25, "4000", "10"),
    )

    assert reasons(series) == [
        "NO_PREVIOUS",
        "NEGATIVE_DELTA",
        "NEGATIVE_DELTA",
        "NEGATIVE_DELTA",
        "COUNTER_RESET",
        None,
    ]
    assert series[-1].short_window is not None
    assert series[-1].short_window.status is ShortWindowStatus.DERIVED
    assert series[-1].windows["5m"].volume == Decimal("1000")


def test_a_counter_back_above_its_level_before_a_reset_is_not_measured_from_the_reset() -> None:
    """Review probe C105: 1M@t0, 900k@t5..t35 (a reset after 30 minutes), 1.005M@t40.
    Real growth is about 5 000 over forty minutes, not a 105 000 spike in five."""

    series = chain(
        RULES,
        (0, "1000000", "100"),
        *((minute, "900000", "100") for minute in range(5, 40, 5)),
        (40, "1005000", "100"),
        (45, "1010000", "100"),
    )

    assert reasons(series)[7] == "COUNTER_RESET"
    resumed = series[8]
    assert "5m" not in resumed.windows  # 5 000 over forty minutes is no 5m window
    assert resumed.short_window is not None
    assert resumed.short_window.reason == "INTERVAL_TOO_LONG"
    # The old counter goes on: the next window is measured from 1 005 000.
    assert series[9].windows["5m"].volume == Decimal("5000")


def test_a_falling_low_counter_restarts_the_reset_clock() -> None:
    rules = ShortWindowRules(counter_reset_after=timedelta(minutes=15))
    series = chain(
        rules,
        (0, "1000000", "2500"),
        (5, "5000", "10"),
        (10, "6000", "12"),
        (15, "100", "1"),  # lower again: not a steady new counter yet
        (20, "200", "2"),
        (25, "300", "3"),
    )

    assert "COUNTER_RESET" not in reasons(series)


def test_zeroed_noise_is_not_counted_again_when_the_counter_recovers() -> None:
    first = live(T0, "1000000", "2500")
    dipped = derive_short_window(live(T0 + timedelta(minutes=5), "999995", "2500"), first, RULES)
    after = derive_short_window(live(T0 + timedelta(minutes=10), "1000300", "2501"), dipped, RULES)

    assert dipped.windows["5m"].volume == Decimal("0")
    assert after.windows["5m"].volume == Decimal("300")  # from 1 000 000, not from 999 995


def test_a_reported_short_window_is_kept_as_the_source_reported_it() -> None:
    previous = live(T0, "1000000", "2500")
    current = live(T0 + timedelta(minutes=5), "1060000", "2650", five_minutes="777")

    derived = derive_short_window(current, previous, RULES)

    assert derived == current
    assert derived.windows["5m"].volume == Decimal("777")
