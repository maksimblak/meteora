"""Immutable screening inputs, policy, and audit-ready decisions."""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.features import VolumeSpikeFeatures
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("screening decimals must not use binary float")
    return value


class CandidateStatus(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"
    WARMING_UP = "WARMING_UP"


class IntegrityStatus(StrEnum):
    CLEAR = "CLEAR"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class RejectionCode(StrEnum):
    INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
    SNAPSHOT_ALIGNMENT_MISMATCH = "SNAPSHOT_ALIGNMENT_MISMATCH"
    FUTURE_MARKET_DATA = "FUTURE_MARKET_DATA"
    FUTURE_TOKEN_RISK_DATA = "FUTURE_TOKEN_RISK_DATA"  # noqa: S105
    FUTURE_FEATURE_DATA = "FUTURE_FEATURE_DATA"
    LOW_TVL = "LOW_TVL"
    LOW_DEPTH = "LOW_DEPTH"
    POOL_TOO_YOUNG = "POOL_TOO_YOUNG"
    HISTORY_WARMING_UP = "HISTORY_WARMING_UP"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    INCOMPLETE_MARKET_DATA = "INCOMPLETE_MARKET_DATA"
    MINT_AUTHORITY_PRESENT = "MINT_AUTHORITY_PRESENT"
    FREEZE_AUTHORITY_PRESENT = "FREEZE_AUTHORITY_PRESENT"
    BLACKLISTED_TOKEN = "BLACKLISTED_TOKEN"  # noqa: S105 - stable domain code
    UNVERIFIED_TOKEN = "UNVERIFIED_TOKEN"  # noqa: S105 - stable domain code
    HOLDER_CONCENTRATION_HIGH = "HOLDER_CONCENTRATION_HIGH"
    TOKEN_RISK_STALE = "TOKEN_RISK_STALE"  # noqa: S105 - stable domain code
    TOKEN_RISK_UNKNOWN = "TOKEN_RISK_UNKNOWN"  # noqa: S105 - stable domain code
    ARTIFICIAL_VOLUME_CONFIRMED = "ARTIFICIAL_VOLUME_CONFIRMED"
    VOLUME_INTEGRITY_UNKNOWN = "VOLUME_INTEGRITY_UNKNOWN"
    PRICE_IMPACT_HIGH = "PRICE_IMPACT_HIGH"
    VOLUME_SIGNAL_BELOW_THRESHOLD = "VOLUME_SIGNAL_BELOW_THRESHOLD"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    # guide-v1 (spec 6.6)
    MARKET_CAP_BELOW_THRESHOLD = "MARKET_CAP_BELOW_THRESHOLD"
    TOKEN_TOO_YOUNG = "TOKEN_TOO_YOUNG"  # noqa: S105 - stable domain code
    VOLUME_PER_MINUTE_BELOW_THRESHOLD = "VOLUME_PER_MINUTE_BELOW_THRESHOLD"
    VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD = "VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD"
    ARTIFICIAL_VOLUME_SUSPECTED = "ARTIFICIAL_VOLUME_SUSPECTED"
    TOTAL_FEES_BELOW_THRESHOLD = "TOTAL_FEES_BELOW_THRESHOLD"
    GUIDE_SIGNAL_UNKNOWN = "GUIDE_SIGNAL_UNKNOWN"
    # live data (D07)
    BLACKLISTED_POOL = "BLACKLISTED_POOL"
    DEPTH_UNAVAILABLE = "DEPTH_UNAVAILABLE"
    VOLUME_PARTICIPANTS_UNAVAILABLE = "VOLUME_PARTICIPANTS_UNAVAILABLE"


class DecisionFlag(StrEnum):
    """Facts the operator must see. A flag never blocks SHADOW; where a stage cannot accept
    it (see each flag), the screener adds the reason of the same name instead."""

    # D03: GMGN-style total fees have no Data API/RPC source; the conditional threshold is
    # skipped and the approval preview asks for a manual check instead.
    TOTAL_FEES_UNAVAILABLE = "TOTAL_FEES_UNAVAILABLE"
    # D07: no quote measured depth and price impact. SHADOW goes on with a conservative
    # slippage model; APPROVAL and LIVE are closed (reason ``DEPTH_UNAVAILABLE``).
    DEPTH_UNAVAILABLE = "DEPTH_UNAVAILABLE"
    # D07: no transaction or participant counts (none from the Data API). Artificial volume
    # is judged on turnover, fee organicity and burst self-similarity; SHADOW and APPROVAL go
    # on, LIVE is closed (reason ``VOLUME_PARTICIPANTS_UNAVAILABLE``) until a provider has them.
    VOLUME_PARTICIPANTS_UNAVAILABLE = "VOLUME_PARTICIPANTS_UNAVAILABLE"


class VolumeIntegrityMode(StrEnum):
    """Which signals decide artificial volume (G07)."""

    # shadow-hypothesis-v1: transaction and participant counts are mandatory.
    PARTICIPANTS = "PARTICIPANTS"
    # Live data (D07): turnover, fee organicity and self-similar bursts are mandatory;
    # participant counts are used when present, otherwise flagged.
    AVAILABLE_SIGNALS = "AVAILABLE_SIGNALS"


class DepthSource(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    QUOTE = "QUOTE"
    UNAVAILABLE = "UNAVAILABLE"


class DepthEvidence(BaseModel):
    """Depth and price impact at the reference size, as measured for the decision (D07).

    ``QUOTE``: the executor's read-only quote; no ``depth`` means the pool could not fill
    the reference size. ``UNAVAILABLE``: nothing was measured, ``detail`` says why.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source: DepthSource
    depth: Decimal | None = None
    price_impact: Decimal | None = None
    reference_notional: Decimal | None = None
    detail: str | None = None
    measured_at: datetime | None = None

    _reject_float = field_validator(
        "depth", "price_impact", "reference_notional", mode="before"
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_depth(self) -> "DepthEvidence":
        if any(
            value is not None and value < 0
            for value in (self.depth, self.price_impact, self.reference_notional)
        ):
            raise ValueError("depth evidence cannot be negative")
        if self.price_impact is not None and self.price_impact > 1:
            raise ValueError("price impact is a fraction in [0, 1]")
        if self.source is DepthSource.UNAVAILABLE and (
            self.depth is not None or self.price_impact is not None
        ):
            raise ValueError("unavailable depth carries no measured value")
        if self.measured_at is not None and (
            self.measured_at.tzinfo is None or self.measured_at.utcoffset() is None
        ):
            raise ValueError("measured_at must be timezone-aware")
        return self


class VolumeRegime(StrEnum):
    BELOW = "BELOW"
    WORKING = "WORKING"
    OVERVOLUME = "OVERVOLUME"
    UNKNOWN = "UNKNOWN"


class DecisionContext(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    mode: TradingMode
    ruleset_version: str
    config_version: str
    correlation_id: str

    @model_validator(mode="after")
    def validate_context(self) -> "DecisionContext":
        if not all((self.ruleset_version, self.config_version, self.correlation_id)):
            raise ValueError("decision versions and correlation_id are required")
        return self


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str
    value: str
    threshold: str | None = None


class ScoreComponent(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str
    normalized_value: Decimal
    weight: Decimal
    contribution: Decimal

    _reject_float = field_validator("normalized_value", "weight", "contribution", mode="before")(
        _exact_decimal
    )


class ScoreWeights(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    volume_acceleration: Decimal = Decimal("0.25")
    robust_volume: Decimal = Decimal("0.20")
    long_confirmation: Decimal = Decimal("0.15")
    fee_opportunity: Decimal = Decimal("0.15")
    liquidity_stability: Decimal = Decimal("0.10")
    volatility_fit: Decimal = Decimal("0.10")
    token_quality: Decimal = Decimal("0.05")

    _reject_float = field_validator("*", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_weights(self) -> "ScoreWeights":
        values = tuple(self.model_dump().values())
        if any(value < 0 for value in values):
            raise ValueError("score weights cannot be negative")
        if sum(values, Decimal(0)) != Decimal(1):
            raise ValueError("score weights must sum exactly to one")
        return self


class GuideScreeningRules(BaseModel):
    """Starting thresholds of the LexDollar guide (spec 6.6, G16) — a shadow hypothesis.

    Top-10 holder share uses ``ScreeningPolicy.maximum_holder_concentration``. Fee
    organicity compares collected fees with ``volume * base fee``: honest DLMM swaps pay
    at least the base fee and the LP share is about 90%, so a ratio far below one means
    the reported volume did not pay for itself.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    minimum_market_cap: Decimal = Decimal("500000")
    preferred_market_cap: Decimal = Decimal("1000000")
    minimum_token_age: timedelta = timedelta(hours=2)
    minimum_volume_per_minute: Decimal = Decimal("14000")
    overvolume_volume_per_minute: Decimal = Decimal("100000")
    minimum_volume_to_liquidity: Decimal = Decimal("0.5")
    minimum_fee_efficiency: Decimal = Decimal("0.5")
    minimum_total_fees_sol: Decimal = Decimal("30")

    _reject_float = field_validator(
        "minimum_market_cap",
        "preferred_market_cap",
        "minimum_volume_per_minute",
        "overvolume_volume_per_minute",
        "minimum_volume_to_liquidity",
        "minimum_fee_efficiency",
        "minimum_total_fees_sol",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_guide(self) -> "GuideScreeningRules":
        if any(
            value < 0
            for value in (
                self.minimum_market_cap,
                self.minimum_volume_per_minute,
                self.minimum_volume_to_liquidity,
                self.minimum_fee_efficiency,
                self.minimum_total_fees_sol,
            )
        ):
            raise ValueError("guide thresholds cannot be negative")
        if self.minimum_token_age < timedelta(0):
            raise ValueError("minimum_token_age cannot be negative")
        if self.preferred_market_cap < self.minimum_market_cap:
            raise ValueError("preferred_market_cap is below minimum_market_cap")
        if self.overvolume_volume_per_minute < self.minimum_volume_per_minute:
            raise ValueError("overvolume threshold is below the working volume threshold")
        return self


class ScreeningPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    minimum_tvl: Decimal
    minimum_depth: Decimal
    minimum_pool_age: timedelta
    maximum_holder_concentration: Decimal
    maximum_price_impact: Decimal
    minimum_short_ratio: Decimal
    minimum_long_ratio: Decimal
    minimum_robust_z_score: Decimal
    minimum_fee_tvl: Decimal
    minimum_score: Decimal
    target_short_ratio: Decimal = Decimal("4")
    target_long_ratio: Decimal = Decimal("2")
    target_robust_z_score: Decimal = Decimal("6")
    target_fee_tvl: Decimal = Decimal("0.01")
    target_volatility: Decimal = Decimal("0.02")
    assumed_cost_rate: Decimal = Decimal("0.002")
    maximum_turnover: Decimal = Decimal("20")
    minimum_unique_participants: int = 5
    maximum_transactions_per_participant: Decimal = Decimal("20")
    weights: ScoreWeights = ScoreWeights()
    guide: GuideScreeningRules | None = None
    volume_integrity: VolumeIntegrityMode = VolumeIntegrityMode.PARTICIPANTS
    # AVAILABLE_SIGNALS: bursts closer than this spread are one repeated trade (G07); the
    # fee organicity below ``minimum_fee_support`` does not pay for the volume it reports.
    maximum_burst_spread: Decimal = Decimal("0.02")
    minimum_fee_support: Decimal = Decimal("0.5")

    _reject_float = field_validator(
        "minimum_tvl",
        "minimum_depth",
        "maximum_holder_concentration",
        "maximum_price_impact",
        "minimum_short_ratio",
        "minimum_long_ratio",
        "minimum_robust_z_score",
        "minimum_fee_tvl",
        "minimum_score",
        "target_short_ratio",
        "target_long_ratio",
        "target_robust_z_score",
        "target_fee_tvl",
        "target_volatility",
        "assumed_cost_rate",
        "maximum_turnover",
        "maximum_transactions_per_participant",
        "maximum_burst_spread",
        "minimum_fee_support",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_policy(self) -> "ScreeningPolicy":
        if not Decimal(0) <= self.maximum_burst_spread < Decimal(1) or self.minimum_fee_support < 0:
            raise ValueError("artificial-volume thresholds are out of range")
        non_negative = (
            self.minimum_tvl,
            self.minimum_depth,
            self.maximum_price_impact,
            self.minimum_short_ratio,
            self.minimum_long_ratio,
            self.minimum_robust_z_score,
            self.minimum_fee_tvl,
            self.minimum_score,
            self.assumed_cost_rate,
            self.maximum_turnover,
        )
        if any(value < 0 for value in non_negative):
            raise ValueError("screening thresholds cannot be negative")
        if not Decimal(0) <= self.maximum_holder_concentration <= Decimal(1):
            raise ValueError("maximum_holder_concentration must be in [0, 1]")
        if not Decimal(0) <= self.maximum_price_impact <= Decimal(1):
            raise ValueError("maximum_price_impact must be in [0, 1]")
        if not Decimal(0) <= self.minimum_score <= Decimal(1):
            raise ValueError("minimum_score must be in [0, 1]")
        if self.minimum_pool_age < timedelta(0):
            raise ValueError("minimum_pool_age cannot be negative")
        if self.minimum_unique_participants < 1:
            raise ValueError("minimum_unique_participants must be positive")
        if any(
            value <= 0
            for value in (
                self.target_short_ratio,
                self.target_long_ratio,
                self.target_robust_z_score,
                self.target_fee_tvl,
                self.target_volatility,
                self.maximum_transactions_per_participant,
            )
        ):
            raise ValueError("normalization and integrity targets must be positive")
        return self


class GuideAssessment(BaseModel):
    """What the guide-v1 thresholds found: terminal reasons, unknown signals and evidence."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    reasons: tuple[RejectionCode, ...]
    missing_signals: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    volume_regime: VolumeRegime
    flags: tuple[DecisionFlag, ...] = ()


class VolumeIntegrityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: IntegrityStatus
    reasons: tuple[str, ...]
    missing_signals: tuple[str, ...]
    flags: tuple[DecisionFlag, ...] = ()


class CandidateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: CandidateStatus
    pool_address: PublicKey
    token_mint: PublicKey
    decision_at: datetime
    mode: TradingMode
    estimated_price_impact: Decimal
    score: Decimal | None
    reasons: tuple[RejectionCode, ...]
    missing_signals: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    score_components: tuple[ScoreComponent, ...]
    features: VolumeSpikeFeatures
    ruleset_version: str
    config_version: str
    correlation_id: str
    flags: tuple[DecisionFlag, ...] = ()
    # How depth and price impact were measured (D07); ``None``: the snapshot's own depth.
    depth: DepthEvidence | None = None

    _reject_float = field_validator("score", "estimated_price_impact", mode="before")(
        _exact_decimal
    )

    @model_validator(mode="after")
    def validate_record(self) -> "CandidateDecision":
        if not all((self.ruleset_version, self.config_version, self.correlation_id)):
            raise ValueError("decision versions and correlation_id are required")
        if self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        if self.decision_at != self.features.decision_at:
            raise ValueError("record and feature decision times differ")
        if self.estimated_price_impact < 0:
            raise ValueError("estimated price impact cannot be negative")
        if self.ruleset_version != self.features.ruleset_version:
            raise ValueError("decision and feature ruleset versions differ")
        if self.config_version != self.features.config_version:
            raise ValueError("decision and feature config versions differ")
        if self.status is CandidateStatus.ACCEPT and self.score is None:
            raise ValueError("accepted decision requires a score")
        if self.score is not None and not Decimal(0) <= self.score <= Decimal(1):
            raise ValueError("decision score must be in [0, 1]")
        return self
