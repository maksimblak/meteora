"""Versioned audit and transactional-outbox event envelope."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class EventType(StrEnum):
    POOL_SNAPSHOT_RECORDED = "PoolSnapshotRecorded"
    CANDIDATE_EVALUATED = "CandidateEvaluated"
    STRATEGY_PLANNED = "StrategyPlanned"
    RISK_REJECTED = "RiskRejected"
    APPROVAL_REQUESTED = "ApprovalRequested"
    COMMAND_SIMULATED = "CommandSimulated"
    COMMAND_SUBMITTED = "CommandSubmitted"
    COMMAND_CONFIRMED = "CommandConfirmed"
    POSITION_OPENED = "PositionOpened"
    POSITION_ACTION_PROPOSED = "PositionActionProposed"
    POSITION_CLOSED = "PositionClosed"
    DAILY_STOP_TRIGGERED = "DailyStopTriggered"
    MODE_CHANGED = "ModeChanged"
    CRITICAL_ALERT_RAISED = "CriticalAlertRaised"


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    occurred_at: datetime
    aggregate_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    schema_version: int
    event_type: EventType
    payload: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value
