"""Immutable staged-mode, approval, daily-risk, entry and kill-switch contracts."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.models.trading import TradingMode

MINIMUM_SHADOW_DAYS = 7
RECOMMENDED_SHADOW_DAYS = 14
MINIMUM_COMPLETED_POSITIONS = 20
MAXIMUM_PROMOTION_DRAWDOWN = Decimal("0.04")
MINIMUM_DATA_COVERAGE = Decimal("0.99")
SYSTEM_ACTOR = "system"


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float is forbidden for risk values")
    return value


def _require_aware(*values: datetime | None) -> None:
    for value in values:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("mode timestamps must be timezone-aware")


class ModeState(BaseModel):
    """Durable mode; ``version`` changes on every mode or safety-flag mutation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    mode: TradingMode = TradingMode.SHADOW
    promotion_authorized: bool = False
    kill_switch_active: bool = False
    daily_stop_date: date | None = None
    version: int = 1

    def daily_stop_active_on(self, risk_date: date) -> bool:
        return self.daily_stop_date == risk_date


class ModeTransition(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    transition_id: UUID
    from_mode: TradingMode
    to_mode: TradingMode
    occurred_at: datetime
    operator_confirmation_id: str
    promotion_authorized: bool
    reason: str = ""
    evidence_id: str | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "ModeTransition":
        _require_aware(self.occurred_at)
        return self


class ShadowReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    allowed: bool
    reasons: tuple[str, ...]
    full_shadow_days: int
    minimum_days: int = MINIMUM_SHADOW_DAYS
    recommended_days: int = RECOMMENDED_SHADOW_DAYS
    minimum_days_remaining: int
    recommended_days_remaining: int
    completed_positions: int
    decision_count: int
    insufficient_statistics: bool
    net_expectancy: Decimal
    net_expectancy_positive: bool
    maximum_drawdown: Decimal
    data_coverage: Decimal
    risk_invariant_breaches: int
    open_critical_alerts: int


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    approval_id: UUID
    command_id: UUID
    command_digest: str
    simulation_digest: str
    operator_id: str
    comment: str = ""
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_grant(self) -> "ApprovalGrant":
        _require_aware(self.issued_at, self.expires_at, self.consumed_at)
        if not self.operator_id.strip():
            raise ValueError("operator_id is required")
        digests = (self.command_digest, self.simulation_digest)
        if any(len(digest) != 64 or set(digest) - set("0123456789abcdef") for digest in digests):
            raise ValueError("approval digests must be sha256 hex strings")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval TTL must be positive")
        if (
            self.consumed_at is not None
            and not self.issued_at <= self.consumed_at < self.expires_at
        ):
            raise ValueError("approval consumption must be inside TTL")
        return self


class RiskDayState(BaseModel):
    """One UTC risk day; the starting equity is fixed when the day is first opened."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    risk_date: date
    starting_equity: Decimal
    baseline_observed_at: datetime
    realized_pnl: Decimal
    conservative_unrealized_pnl: Decimal
    daily_stop_triggered: bool
    triggered_at: datetime | None = None
    ruleset_version: str
    updated_at: datetime

    _reject_float = field_validator(
        "starting_equity", "realized_pnl", "conservative_unrealized_pnl", mode="before"
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_risk_day(self) -> "RiskDayState":
        _require_aware(self.baseline_observed_at, self.triggered_at, self.updated_at)
        if self.starting_equity <= 0:
            raise ValueError("starting equity must be positive")
        if not self.ruleset_version:
            raise ValueError("ruleset_version is required")
        if self.daily_stop_triggered != (self.triggered_at is not None):
            raise ValueError("triggered_at must be set exactly when the stop is triggered")
        return self

    @property
    def conservative_pnl(self) -> Decimal:
        return self.realized_pnl + min(self.conservative_unrealized_pnl, Decimal(0))


class PortfolioEquitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    observed_at: datetime
    equity: Decimal
    ruleset_version: str

    _reject_float = field_validator("equity", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PortfolioEquitySnapshot":
        _require_aware(self.observed_at)
        if self.equity <= 0 or not self.ruleset_version:
            raise ValueError("portfolio snapshot requires positive equity and ruleset")
        return self


class DailyPromotionObservation(BaseModel):
    """Immutable facts of one complete UTC day of a (ruleset, mode) population."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    risk_date: date
    ruleset_version: str
    mode: TradingMode
    completed_positions: int
    net_pnl: Decimal
    start_equity: Decimal
    end_equity: Decimal
    conservative_open_equity: Decimal
    data_coverage: Decimal
    decision_count: int = 0
    risk_invariant_breaches: int = 0

    _reject_float = field_validator(
        "net_pnl",
        "start_equity",
        "end_equity",
        "conservative_open_equity",
        "data_coverage",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_observation(self) -> "DailyPromotionObservation":
        if not self.ruleset_version or min(
            self.completed_positions, self.decision_count, self.risk_invariant_breaches
        ) < 0:
            raise ValueError("daily observation identity or counters are invalid")
        if min(self.start_equity, self.end_equity, self.conservative_open_equity) <= 0:
            raise ValueError("daily observation equity must be positive")
        if not Decimal(0) <= self.data_coverage <= Decimal(1):
            raise ValueError("data coverage must be in [0, 1]")
        return self


class PromotionEvidenceSnapshot(BaseModel):
    """Versioned, persisted, one-shot evidence bound to the mode version it was built on."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    evidence_id: UUID
    ruleset_version: str
    from_mode: TradingMode
    mode_version: int
    generated_at: datetime
    first_date: date | None
    through_date: date
    full_utc_days: int
    completed_positions: int
    decision_count: int
    net_expectancy: Decimal
    maximum_drawdown: Decimal
    data_coverage: Decimal
    risk_invariant_breaches: int
    open_critical_alerts: int
    consumed_at: datetime | None = None

    _reject_float = field_validator(
        "net_expectancy", "maximum_drawdown", "data_coverage", mode="before"
    )(_exact_decimal)


class PendingPlanStatus(StrEnum):
    """PENDING -> EXECUTING -> OPENED | FAILED; only PENDING may be CANCELLED."""

    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    OPENED = "OPENED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PendingPlan(BaseModel):
    """An admitted, not yet confirmed entry; it counts against the position cap."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    plan_id: UUID
    correlation_id: str
    admitted_at: datetime
    expires_at: datetime
    position_id: str | None = None
    status: PendingPlanStatus = PendingPlanStatus.PENDING

    @model_validator(mode="after")
    def validate_plan(self) -> "PendingPlan":
        _require_aware(self.admitted_at, self.expires_at)
        if self.expires_at <= self.admitted_at:
            raise ValueError("pending plan must expire after admission")
        if not self.correlation_id:
            raise ValueError("correlation_id is required")
        return self


class EntryDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    admitted: bool
    plan_id: UUID
    reasons: tuple[str, ...]
    correlation_id: str


class KillSwitchPolicy(StrEnum):
    HOLD_AND_ALERT = "HOLD_AND_ALERT"
    ORDERLY_EXIT = "ORDERLY_EXIT"


class KillSwitchActionKind(StrEnum):
    HOLD_AND_ALERT = "HOLD_AND_ALERT"
    ORDERLY_EXIT_REQUIRED = "ORDERLY_EXIT_REQUIRED"


class KillSwitchPositionAction(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    position_id: str
    action: KillSwitchActionKind
    transaction_submitted: bool = False

    @model_validator(mode="after")
    def reject_panic_transaction(self) -> "KillSwitchPositionAction":
        if self.transaction_submitted:
            raise ValueError("kill switch cannot submit panic transactions")
        return self


class KillSwitchResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    activated_at: datetime
    reason: str
    policy: KillSwitchPolicy
    cancelled_plan_count: int
    position_actions: tuple[KillSwitchPositionAction, ...]
    alert_id: UUID


class CriticalAlert(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    alert_id: UUID
    code: str
    message: str
    created_at: datetime
    ruleset_version: str = SYSTEM_ACTOR
    open: bool = True
    resolved_at: datetime | None = None
    resolution: str = ""

    @model_validator(mode="after")
    def validate_alert(self) -> "CriticalAlert":
        _require_aware(self.created_at, self.resolved_at)
        if self.open == (self.resolved_at is not None):
            raise ValueError("resolved_at must be set exactly when the alert is resolved")
        return self
