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

    _reject_float = field_validator("zero_dispersion_score", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_rules(self) -> "VolumeFeatureRules":
        if not self.ruleset_version or not self.config_version:
            raise ValueError("ruleset and config versions are required")
        if self.short_window not in {"5m", "30m", "1h", "4h", "24h"}:
            raise ValueError("unsupported short_window")
        if self.long_window not in {"5m", "30m", "1h", "4h", "24h"}:
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
