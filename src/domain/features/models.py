"""Immutable feature contracts used by screening and strategy planning."""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.models.primitives import PublicKey
from src.market.models import DataQuality, Freshness


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float is forbidden in feature rules")
    return value


class SignalReadiness(StrEnum):
    WARMING_UP = "WARMING_UP"
    READY = "READY"


WINDOW_MINUTES: dict[str, int] = {"5m": 5, "30m": 30, "1h": 60, "4h": 240, "24h": 1_440}


class GuideFeatureRules(BaseModel):
    """Inputs of the LexDollar guide rules (spec 6.6); all are point-in-time.

    ``volume_window`` feeds volume per minute; volume-to-liquidity is that rate over five
    minutes, as the guide reads it, whatever the window. ``organicity_window`` compares
    collected fees with the pool's base fee. A local high looks back
    ``local_high_lookback``. The pool ATH is known only when the stored history starts
    within ``ath_coverage_tolerance`` of pool creation and no two consecutive snapshots are
    further apart than ``maximum_observation_gap`` (the 5-minute universe cadence plus
    scheduling slack).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    volume_window: str = "5m"
    organicity_window: str = "1h"
    local_high_lookback: timedelta = timedelta(hours=1)
    ath_coverage_tolerance: timedelta = timedelta(minutes=15)
    maximum_observation_gap: timedelta = timedelta(minutes=5, seconds=30)

    @model_validator(mode="after")
    def validate_guide(self) -> "GuideFeatureRules":
        if self.volume_window not in WINDOW_MINUTES or self.organicity_window not in WINDOW_MINUTES:
            raise ValueError("unsupported guide window")
        if self.local_high_lookback <= timedelta(0):
            raise ValueError("local_high_lookback must be positive")
        if self.ath_coverage_tolerance < timedelta(0):
            raise ValueError("ath_coverage_tolerance cannot be negative")
        if self.maximum_observation_gap <= timedelta(0):
            raise ValueError("maximum_observation_gap must be positive")
        return self


class VolumeFeatureRules(BaseModel):
    """Versioned hypotheses; defaults are for shadow validation, not profit claims."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    ruleset_version: str
    config_version: str
    short_window: str = "5m"
    long_window: str = "30m"
    baseline_points: int = 12
    minimum_baseline_points: int = 6
    maximum_history_points: int = 288
    maximum_source_age: timedelta = timedelta(minutes=10)
    zero_dispersion_score: Decimal = Decimal("10")
    guide: GuideFeatureRules | None = None

    _reject_float = field_validator("zero_dispersion_score", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_rules(self) -> "VolumeFeatureRules":
        if not self.ruleset_version or not self.config_version:
            raise ValueError("ruleset and config versions are required")
        if self.short_window not in WINDOW_MINUTES:
            raise ValueError("unsupported short_window")
        if self.long_window not in WINDOW_MINUTES:
            raise ValueError("unsupported long_window")
        if self.baseline_points < 1:
            raise ValueError("baseline_points must be positive")
        if not 2 <= self.minimum_baseline_points <= self.baseline_points:
            raise ValueError("minimum_baseline_points must be between 2 and baseline_points")
        if self.maximum_history_points < self.baseline_points + 1:
            raise ValueError("maximum_history_points must include current and baseline")
        if self.maximum_source_age <= timedelta(0):
            raise ValueError("maximum_source_age must be positive")
        if self.zero_dispersion_score <= 0:
            raise ValueError("zero_dispersion_score must be positive")
        return self


class VolumeSpikeFeatures(BaseModel):
    """Point-in-time feature vector with no data newer than ``decision_at``."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_at: datetime
    observed_at: datetime
    source_at: datetime
    ingested_at: datetime
    pool_address: PublicKey
    base_mint: PublicKey
    quote_mint: PublicKey
    baseline_points: int
    readiness: SignalReadiness
    current_short_volume: Decimal | None
    current_long_volume: Decimal | None
    short_median: Decimal | None
    long_median: Decimal | None
    short_ratio: Decimal | None
    long_ratio: Decimal | None
    robust_z_score: Decimal | None
    fee_tvl: Decimal | None
    tvl_change: Decimal | None
    price_volatility: Decimal | None
    price_momentum: Decimal | None
    turnover: Decimal | None
    transaction_count: int | None
    unique_participants: int | None
    freshness: Freshness
    data_quality: DataQuality
    missing_signals: tuple[str, ...]
    ruleset_version: str
    config_version: str
    # guide-v1 inputs; ``None`` when the ruleset does not compute them or a source is
    # missing. They never enter ``missing_signals``: screening owns the guide UNKNOWN rule.
    volume_per_minute: Decimal | None = None
    volume_to_liquidity: Decimal | None = None
    fee_efficiency: Decimal | None = None
    local_high_price: Decimal | None = None
    all_time_high_price: Decimal | None = None

    _reject_float = field_validator(
        "volume_per_minute",
        "volume_to_liquidity",
        "fee_efficiency",
        "local_high_price",
        "all_time_high_price",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_point_in_time(self) -> "VolumeSpikeFeatures":
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (
                self.decision_at,
                self.observed_at,
                self.source_at,
                self.ingested_at,
            )
        ):
            raise ValueError("feature timestamps must be timezone-aware")
        if self.baseline_points < 0:
            raise ValueError("baseline_points cannot be negative")
        return self
