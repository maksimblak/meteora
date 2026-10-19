"""Stable, machine-readable market quality gates."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from src.market.features import FeatureReadiness, FeatureVector
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)


class MarketReasonCode(StrEnum):
    LOW_TVL = "LOW_TVL"
    LOW_DEPTH = "LOW_DEPTH"
    POOL_TOO_YOUNG = "POOL_TOO_YOUNG"
    INCOMPLETE_HISTORY = "INCOMPLETE_HISTORY"
    STALE_DATA = "STALE_DATA"
    INCOMPLETE_DATA = "INCOMPLETE_DATA"


class HardFilterStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - domain decision status, not a password
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


class RiskReasonCode(StrEnum):
    MINT_AUTHORITY_PRESENT = "MINT_AUTHORITY_PRESENT"
    FREEZE_AUTHORITY_PRESENT = "FREEZE_AUTHORITY_PRESENT"
    BLACKLISTED = "BLACKLISTED"
    UNVERIFIED_TOKEN = "UNVERIFIED_TOKEN"  # noqa: S105 - stable reason code
    HOLDER_CONCENTRATION_HIGH = "HOLDER_CONCENTRATION_HIGH"
    WASH_TRADING_DETECTED = "WASH_TRADING_DETECTED"
    ARTIFICIAL_VOLUME = "ARTIFICIAL_VOLUME"


@dataclass(frozen=True, slots=True)
class HardFilterPolicy:
    maximum_holder_concentration: Decimal
    maximum_volume_acceleration: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.maximum_holder_concentration, float) or isinstance(
            self.maximum_volume_acceleration, float
        ):
            raise ValueError("hard-filter thresholds must use Decimal")
        if not Decimal(0) <= self.maximum_holder_concentration <= Decimal(1):
            raise ValueError("maximum_holder_concentration must be between zero and one")
        if self.maximum_volume_acceleration < 0:
            raise ValueError("maximum_volume_acceleration must not be negative")


@dataclass(frozen=True, slots=True)
class HardFilterAssessment:
    status: HardFilterStatus
    reasons: tuple[RiskReasonCode, ...]
    missing_signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketQualityPolicy:
    minimum_tvl: Decimal
    minimum_depth: Decimal
    minimum_pool_age: timedelta
    required_history_points: int

    def __post_init__(self) -> None:
        if isinstance(self.minimum_tvl, float) or isinstance(self.minimum_depth, float):
            raise ValueError("market quality thresholds must use Decimal")
        if self.minimum_tvl < 0 or self.minimum_depth < 0:
            raise ValueError("market quality thresholds must not be negative")
        if self.minimum_pool_age < timedelta(0):
            raise ValueError("minimum_pool_age must not be negative")
        if self.required_history_points < 1:
            raise ValueError("required_history_points must be at least one")


@dataclass(frozen=True, slots=True)
class MarketQualityAssessment:
    eligible: bool
    reasons: tuple[MarketReasonCode, ...]


def assess_market_quality(
    snapshot: PoolSnapshot,
    features: FeatureVector,
    policy: MarketQualityPolicy,
) -> MarketQualityAssessment:
    reasons: list[MarketReasonCode] = []
    if snapshot.tvl < policy.minimum_tvl:
        reasons.append(MarketReasonCode.LOW_TVL)
    if snapshot.depth is None or snapshot.depth < policy.minimum_depth:
        reasons.append(MarketReasonCode.LOW_DEPTH)
    if snapshot.pool_age < policy.minimum_pool_age:
        reasons.append(MarketReasonCode.POOL_TOO_YOUNG)
    if (
        features.readiness is FeatureReadiness.WARMING_UP
        or features.history_points < policy.required_history_points
    ):
        reasons.append(MarketReasonCode.INCOMPLETE_HISTORY)
    if snapshot.freshness is Freshness.STALE:
        reasons.append(MarketReasonCode.STALE_DATA)
    if snapshot.data_quality is not DataQuality.COMPLETE:
        reasons.append(MarketReasonCode.INCOMPLETE_DATA)
    return MarketQualityAssessment(eligible=not reasons, reasons=tuple(reasons))


def assess_hard_filters(
    token: TokenRiskSnapshot,
    features: FeatureVector,
    policy: HardFilterPolicy,
) -> HardFilterAssessment:
    """Fail closed on explicit danger and preserve unresolved mandatory signals."""

    reasons: list[RiskReasonCode] = []
    if token.mint_authority is AuthorityStatus.PRESENT:
        reasons.append(RiskReasonCode.MINT_AUTHORITY_PRESENT)
    if token.freeze_authority is AuthorityStatus.PRESENT:
        reasons.append(RiskReasonCode.FREEZE_AUTHORITY_PRESENT)
    if token.blacklisted is RiskSignal.UNSAFE:
        reasons.append(RiskReasonCode.BLACKLISTED)
    if token.verified is RiskSignal.UNSAFE:
        reasons.append(RiskReasonCode.UNVERIFIED_TOKEN)
    if (
        token.holder_concentration is not None
        and token.holder_concentration > policy.maximum_holder_concentration
    ):
        reasons.append(RiskReasonCode.HOLDER_CONCENTRATION_HIGH)
    if token.wash_trading is RiskSignal.UNSAFE:
        reasons.append(RiskReasonCode.WASH_TRADING_DETECTED)
    if (
        features.volume_acceleration is not None
        and features.volume_acceleration > policy.maximum_volume_acceleration
    ):
        reasons.append(RiskReasonCode.ARTIFICIAL_VOLUME)

    missing = set(token.missing_signals)
    if token.freshness is Freshness.STALE:
        missing.add("risk_freshness")
    if token.data_quality is not DataQuality.COMPLETE and not token.missing_signals:
        missing.add("risk_data_quality")
    if features.volume_acceleration is None:
        missing.add("volume_acceleration")

    if reasons:
        status = HardFilterStatus.REJECT
    elif missing:
        status = HardFilterStatus.UNKNOWN
    else:
        status = HardFilterStatus.PASS
    return HardFilterAssessment(
        status=status,
        reasons=tuple(reasons),
        missing_signals=tuple(sorted(missing)),
    )
