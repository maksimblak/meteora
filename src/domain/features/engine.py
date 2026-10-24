"""Robust volume-spike features computed from historical snapshots only."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.domain.features.models import (
    WINDOW_MINUTES,
    GuideFeatureRules,
    SignalReadiness,
    VolumeFeatureRules,
    VolumeSpikeFeatures,
)
from src.market.models import (
    DataQuality,
    Freshness,
    PoolSnapshot,
    VolumeBucket,
    WindowMetrics,
    WindowOrigin,
)

_TURNOVER_MINUTES = 5
MAD_SCALE = Decimal("1.4826")
IQR_SCALE = Decimal("1.349")


class VolumeFeatureEngine:
    """Build a reproducible point-in-time vector without look-ahead leakage."""

    def compute(
        self,
        history: Sequence[PoolSnapshot],
        rules: VolumeFeatureRules,
        *,
        decision_at: datetime,
        warmup: Sequence[VolumeBucket] = (),
    ) -> VolumeSpikeFeatures:
        """``warmup``: closed short buckets of the pool's volume history (D07).

        They stand in for the baseline only while the pool's own snapshots carry fewer
        than ``minimum_baseline_points`` short windows, and for the current short window
        only when the latest snapshot has none. A bucket is used only if it ended by
        ``decision_at`` and before the current window began.
        """

        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        eligible = [
            item
            for item in history
            if item.observed_at <= decision_at
            and item.source_at <= decision_at
            and item.ingested_at <= decision_at
        ]
        if not eligible:
            raise ValueError("no point-in-time snapshots exist at decision_at")
        ordered = sorted(eligible, key=lambda item: (item.observed_at, item.ingested_at))
        ordered = ordered[-rules.maximum_history_points :]
        latest = ordered[-1]
        if any(item.pool != latest.pool for item in ordered):
            raise ValueError("feature history must belong to one pool")
        if any(bucket.pool_address != latest.pool.address for bucket in warmup):
            raise ValueError("volume history must belong to one pool")
        _reject_conflicting_observations(ordered)
        buckets = _closed_buckets(warmup, rules, decision_at)

        prior = ordered[:-1][-rules.baseline_points :]
        short = _current_short(latest, buckets, rules, decision_at)
        current_short = short.metrics if short is not None else None
        current_long = latest.windows.get(rules.long_window)
        short_values = _window_values(prior, rules.short_window)
        long_values = _window_values(prior, rules.long_window)
        baseline_origin = _snapshot_origin(prior, rules.short_window) if short_values else None
        if len(short_values) < rules.minimum_baseline_points and buckets:
            short_end = short.end if short is not None else latest.source_at
            history_short = _bucket_values(buckets, short_end - _window(rules.short_window))
            if len(history_short) > len(short_values):
                short_values = history_short[-rules.baseline_points :]
                baseline_origin = WindowOrigin.VOLUME_HISTORY
        if len(long_values) < rules.minimum_baseline_points and buckets:
            history_long = _rolling_sums(
                buckets, rules, latest.source_at - _window(rules.short_window)
            )
            if len(history_long) > len(long_values):
                long_values = history_long[-rules.baseline_points :]
        missing: set[str] = set()

        short_median = _median(short_values)
        long_median = _median(long_values)
        short_ratio = _ratio(current_short, short_median, "short_ratio", missing)
        long_ratio = _ratio(current_long, long_median, "long_ratio", missing)
        robust_z = _robust_score(current_short, short_values, rules, missing)
        fee_tvl = _fee_tvl(current_long, latest.tvl, missing)
        turnover = _turnover(current_long, latest.tvl, missing)
        tvl_change = _relative_change(prior[-1].tvl, latest.tvl) if prior else None
        price_momentum = _relative_change(prior[-1].price, latest.price) if prior else None
        price_volatility = _price_volatility(ordered, missing)
        if tvl_change is None:
            missing.add("tvl_change")
        if price_momentum is None:
            missing.add("price_momentum")

        enough_baseline = (
            len(short_values) >= rules.minimum_baseline_points
            and len(long_values) >= rules.minimum_baseline_points
        )
        readiness = SignalReadiness.READY if enough_baseline else SignalReadiness.WARMING_UP
        if not enough_baseline:
            missing.add("baseline_history")

        freshness = latest.freshness
        if decision_at - latest.source_at > rules.maximum_source_age:
            freshness = Freshness.STALE
            missing.add("freshness")
        quality = (
            DataQuality.COMPLETE
            if readiness is SignalReadiness.READY
            and freshness is Freshness.FRESH
            and not missing
            and all(item.data_quality is DataQuality.COMPLETE for item in ordered)
            else DataQuality.PARTIAL
        )
        guide = _guide_features(
            ordered,
            rules.guide,
            decision_at,
            current_short if rules.short_window == _guide_window(rules) else None,
            rules.short_window,
        )
        return VolumeSpikeFeatures(
            decision_at=decision_at,
            observed_at=latest.observed_at,
            source_at=latest.source_at,
            ingested_at=latest.ingested_at,
            pool_address=latest.pool.address,
            base_mint=latest.pool.base_mint,
            quote_mint=latest.pool.quote_mint,
            baseline_points=min(len(short_values), len(long_values)),
            readiness=readiness,
            current_short_volume=current_short.volume if current_short else None,
            current_long_volume=current_long.volume if current_long else None,
            short_median=short_median,
            long_median=long_median,
            short_ratio=short_ratio,
            long_ratio=long_ratio,
            robust_z_score=robust_z,
            fee_tvl=fee_tvl,
            tvl_change=tvl_change,
            price_volatility=price_volatility,
            price_momentum=price_momentum,
            turnover=turnover,
            transaction_count=current_short.transaction_count if current_short else None,
            unique_participants=current_short.unique_participants if current_short else None,
            freshness=freshness,
            data_quality=quality,
            missing_signals=tuple(sorted(missing)),
            ruleset_version=rules.ruleset_version,
            config_version=rules.config_version,
            volume_per_minute=guide.volume_per_minute,
            volume_to_liquidity=guide.volume_to_liquidity,
            fee_efficiency=guide.fee_efficiency,
            local_high_price=guide.local_high_price,
            all_time_high_price=guide.all_time_high_price,
            volume_window_source=guide.volume_window_source,
            hourly_volume_per_minute=guide.hourly_volume_per_minute,
            hourly_turnover=guide.hourly_turnover,
            short_window_origin=short.origin if short is not None else None,
            baseline_origin=baseline_origin,
            short_burst_spread=_burst_spread(
                [*short_values, current_short.volume] if current_short is not None else [],
                rules.burst_points,
            ),
        )


@dataclass(frozen=True, slots=True)
class _ShortWindow:
    """The current short window, where it came from and when it ended."""

    metrics: WindowMetrics
    origin: WindowOrigin
    end: datetime


def _window(name: str) -> timedelta:
    return timedelta(minutes=WINDOW_MINUTES[name])


def _closed_buckets(
    warmup: Sequence[VolumeBucket], rules: VolumeFeatureRules, decision_at: datetime
) -> list[VolumeBucket]:
    """Short-length buckets that ended by the decision, oldest first, one per start."""

    width = _window(rules.short_window)
    by_start = {
        bucket.start: bucket
        for bucket in warmup
        if bucket.duration == width and bucket.end <= decision_at
    }
    return [by_start[start] for start in sorted(by_start)]


def _current_short(
    latest: PoolSnapshot,
    buckets: Sequence[VolumeBucket],
    rules: VolumeFeatureRules,
    decision_at: datetime,
) -> _ShortWindow | None:
    reported = latest.windows.get(rules.short_window)
    if reported is not None:
        derivation = latest.short_window
        origin = (
            derivation.origin
            if derivation is not None and derivation.window == rules.short_window
            else WindowOrigin.REPORTED
        )
        return _ShortWindow(reported, origin, latest.source_at)
    if not buckets:
        return None
    last = buckets[-1]
    if decision_at - last.end > rules.maximum_source_age:
        return None
    metrics = WindowMetrics(volume=last.volume, fees=last.fees, protocol_fees=last.protocol_fees)
    return _ShortWindow(metrics, WindowOrigin.VOLUME_HISTORY, last.end)


def _snapshot_origin(prior: Sequence[PoolSnapshot], window: str) -> WindowOrigin:
    derived = any(
        item.short_window is not None
        and item.short_window.window == window
        and window in item.windows
        for item in prior
    )
    return WindowOrigin.CUMULATIVE_DELTA if derived else WindowOrigin.REPORTED


def _bucket_values(buckets: Sequence[VolumeBucket], until: datetime) -> list[Decimal]:
    return [bucket.volume for bucket in buckets if bucket.end <= until]


def _rolling_sums(
    buckets: Sequence[VolumeBucket], rules: VolumeFeatureRules, until: datetime
) -> list[Decimal]:
    """Long-window volumes as sums of consecutive short buckets ending by ``until``."""

    span = WINDOW_MINUTES[rules.long_window] // WINDOW_MINUTES[rules.short_window]
    usable = [bucket for bucket in buckets if bucket.end <= until]
    sums: list[Decimal] = []
    for index in range(span - 1, len(usable)):
        run = usable[index - span + 1 : index + 1]
        contiguous = all(
            later.start == earlier.end for earlier, later in zip(run, run[1:], strict=False)
        )
        if contiguous:
            sums.append(sum((bucket.volume for bucket in run), Decimal(0)))
    return sums


def _burst_spread(series: Sequence[Decimal], points: int) -> Decimal | None:
    recent = list(series[-points:])
    if len(recent) < points:
        return None
    high = max(recent)
    if high <= 0:
        return None
    return (high - min(recent)) / high


def _guide_window(rules: VolumeFeatureRules) -> str | None:
    return None if rules.guide is None else rules.guide.volume_window


@dataclass(frozen=True, slots=True)
class _GuideValues:
    volume_per_minute: Decimal | None = None
    volume_to_liquidity: Decimal | None = None
    fee_efficiency: Decimal | None = None
    local_high_price: Decimal | None = None
    all_time_high_price: Decimal | None = None
    volume_window_source: str | None = None
    hourly_volume_per_minute: Decimal | None = None
    hourly_turnover: Decimal | None = None


def _guide_features(
    ordered: Sequence[PoolSnapshot],
    rules: GuideFeatureRules | None,
    decision_at: datetime,
    current_short: WindowMetrics | None,
    short_window: str,
) -> _GuideValues:
    """guide-v1 inputs from point-in-time windows; every quantity stays ``None`` if unknown.

    ``current_short`` is the resolved current short window (reported, derived or the last
    history bucket); it is the volume window whenever the guide reads that window.
    """

    if rules is None:
        return _GuideValues()
    latest = ordered[-1]
    window: str | None
    volume: WindowMetrics | None
    if current_short is not None:
        window, volume = short_window, current_short
    else:
        window = _volume_window(latest, rules)
        volume = latest.windows.get(window) if window is not None else None
    per_minute = (
        volume.volume / Decimal(WINDOW_MINUTES[window])
        if volume is not None and window is not None
        else None
    )
    # The guide threshold is 5-minute volume to liquidity; rescale so a longer window
    # (a 30m average) cannot make the 0.5 bar several times easier to clear.
    to_liquidity = (
        per_minute * Decimal(_TURNOVER_MINUTES) / latest.tvl
        if per_minute is not None and latest.tvl > 0
        else None
    )
    organic = latest.windows.get(rules.organicity_window)
    fee_efficiency = None
    # Organicity compares every fee paid (LP fees plus the protocol share, D07) with what
    # the volume would have paid at the pool's base fee.
    paid = organic.total_fees if organic is not None else None
    if (
        organic is not None
        and paid is not None
        and organic.volume > 0
        and latest.base_fee_rate is not None
        and latest.base_fee_rate > 0
    ):
        fee_efficiency = paid / (organic.volume * latest.base_fee_rate)
    window_start = decision_at - rules.local_high_lookback
    local_high = max(item.price for item in ordered if item.observed_at >= window_start)
    prior = ordered[:-1]
    # An ATH is proven only by a history observed from pool creation without holes: a
    # peak inside an unobserved gap would make any later "breakout" a lower high.
    covers_pool_life = (
        bool(prior)
        and prior[0].observed_at - latest.created_at <= rules.ath_coverage_tolerance
        and all(
            current.observed_at - previous.observed_at <= rules.maximum_observation_gap
            for previous, current in zip(ordered, ordered[1:], strict=False)
        )
    )
    all_time_high = max(item.price for item in prior) if covers_pool_life else None
    hour = latest.windows.get(rules.sustained_window)
    return _GuideValues(
        volume_per_minute=per_minute,
        volume_to_liquidity=to_liquidity,
        fee_efficiency=fee_efficiency,
        local_high_price=local_high,
        all_time_high_price=all_time_high,
        volume_window_source=window if per_minute is not None else None,
        hourly_volume_per_minute=(
            hour.volume / Decimal(WINDOW_MINUTES[rules.sustained_window])
            if hour is not None
            else None
        ),
        hourly_turnover=hour.volume / latest.tvl if hour is not None and latest.tvl > 0 else None,
    )


def _volume_window(latest: PoolSnapshot, rules: GuideFeatureRules) -> str | None:
    """The configured window, else the source's marked substitute within the limit (D04)."""

    if rules.volume_window in latest.windows:
        return rules.volume_window
    substitute = latest.short_window_source
    if (
        substitute is None
        or substitute not in WINDOW_MINUTES
        or WINDOW_MINUTES[substitute] > WINDOW_MINUTES[rules.maximum_volume_window]
    ):
        return None
    return substitute


def _reject_conflicting_observations(history: Sequence[PoolSnapshot]) -> None:
    for previous, current in zip(history, history[1:], strict=False):
        if previous.observed_at == current.observed_at and previous != current:
            raise ValueError("conflicting snapshots share observed_at")


def _window_values(history: Sequence[PoolSnapshot], name: str) -> list[Decimal]:
    return [item.windows[name].volume for item in history if name in item.windows]


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _ratio(
    current: WindowMetrics | None,
    baseline: Decimal | None,
    signal: str,
    missing: set[str],
) -> Decimal | None:
    if current is None or baseline is None or baseline <= 0:
        missing.add(signal)
        return None
    return current.volume / baseline


def _robust_score(
    current: WindowMetrics | None,
    baseline: Sequence[Decimal],
    rules: VolumeFeatureRules,
    missing: set[str],
) -> Decimal | None:
    center = _median(baseline)
    if current is None or center is None or len(baseline) < rules.minimum_baseline_points:
        missing.add("robust_z_score")
        return None
    deviations = [abs(value - center) for value in baseline]
    mad = _median(deviations)
    if mad is not None and mad > 0:
        return (current.volume - center) / (MAD_SCALE * mad)
    q1, q3 = _quartiles(baseline)
    if q1 is not None and q3 is not None and q3 > q1:
        return (current.volume - center) / ((q3 - q1) / IQR_SCALE)
    if current.volume == center:
        return Decimal(0)
    direction = Decimal(1) if current.volume > center else Decimal(-1)
    return direction * rules.zero_dispersion_score


def _quartiles(values: Sequence[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if len(values) < 4:
        return None, None
    ordered = sorted(values)
    middle = len(ordered) // 2
    lower = ordered[:middle]
    upper = ordered[middle:] if len(ordered) % 2 == 0 else ordered[middle + 1 :]
    return _median(lower), _median(upper)


def _fee_tvl(
    current: WindowMetrics | None,
    tvl: Decimal,
    missing: set[str],
) -> Decimal | None:
    if current is None or current.fees is None or tvl <= 0:
        missing.add("fee_tvl")
        return None
    return current.fees / tvl


def _turnover(
    current: WindowMetrics | None,
    tvl: Decimal,
    missing: set[str],
) -> Decimal | None:
    if current is None or tvl <= 0:
        missing.add("turnover")
        return None
    return current.volume / tvl


def _relative_change(previous: Decimal, current: Decimal) -> Decimal | None:
    if previous <= 0:
        return None
    return (current - previous) / previous


def _price_volatility(
    history: Sequence[PoolSnapshot],
    missing: set[str],
) -> Decimal | None:
    moves: list[Decimal] = []
    for previous, current in zip(history, history[1:], strict=False):
        change = _relative_change(previous.price, current.price)
        if change is None:
            missing.add("price_volatility")
            return None
        moves.append(abs(change))
    if not moves:
        missing.add("price_volatility")
        return None
    return _median(moves)
