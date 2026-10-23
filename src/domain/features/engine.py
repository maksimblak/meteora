"""Robust volume-spike features computed from historical snapshots only."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.domain.features.models import (
    WINDOW_MINUTES,
    GuideFeatureRules,
    SignalReadiness,
    VolumeFeatureRules,
    VolumeSpikeFeatures,
)
from src.market.models import DataQuality, Freshness, PoolSnapshot, WindowMetrics

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
    ) -> VolumeSpikeFeatures:
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
        _reject_conflicting_observations(ordered)

        prior = ordered[:-1][-rules.baseline_points :]
        short_values = _window_values(prior, rules.short_window)
        long_values = _window_values(prior, rules.long_window)
        current_short = latest.windows.get(rules.short_window)
        current_long = latest.windows.get(rules.long_window)
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
        guide = _guide_features(ordered, rules.guide, decision_at)
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
        )


@dataclass(frozen=True, slots=True)
class _GuideValues:
    volume_per_minute: Decimal | None = None
    volume_to_liquidity: Decimal | None = None
    fee_efficiency: Decimal | None = None
    local_high_price: Decimal | None = None
    all_time_high_price: Decimal | None = None


def _guide_features(
    ordered: Sequence[PoolSnapshot],
    rules: GuideFeatureRules | None,
    decision_at: datetime,
) -> _GuideValues:
    """guide-v1 inputs from point-in-time windows; every quantity stays ``None`` if unknown."""

    if rules is None:
        return _GuideValues()
    latest = ordered[-1]
    volume = latest.windows.get(rules.volume_window)
    per_minute = volume.volume / Decimal(WINDOW_MINUTES[rules.volume_window]) if volume else None
    # The guide threshold is 5-minute volume to liquidity; rescale so a longer window
    # (a 30m average) cannot make the 0.5 bar several times easier to clear.
    to_liquidity = (
        per_minute * Decimal(_TURNOVER_MINUTES) / latest.tvl
        if per_minute is not None and latest.tvl > 0
        else None
    )
    organic = latest.windows.get(rules.organicity_window)
    fee_efficiency = None
    if (
        organic is not None
        and organic.fees is not None
        and organic.volume > 0
        and latest.base_fee_rate is not None
        and latest.base_fee_rate > 0
    ):
        fee_efficiency = organic.fees / (organic.volume * latest.base_fee_rate)
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
    return _GuideValues(
        volume_per_minute=per_minute,
        volume_to_liquidity=to_liquidity,
        fee_efficiency=fee_efficiency,
        local_high_price=local_high,
        all_time_high_price=all_time_high,
    )


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
