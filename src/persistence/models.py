"""Database-only records; domain code never imports this module.

The schema is owned by Alembic (``migrations/versions``); these mappings mirror it
so repositories can build typed statements.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PoolRecord(Base):
    __tablename__ = "pools"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    address: Mapped[str] = mapped_column(String(44), unique=True, nullable=False)
    base_mint: Mapped[str] = mapped_column(String(44), nullable=False)
    quote_mint: Mapped[str] = mapped_column(String(44), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TokenRiskSnapshotRecord(Base):
    __tablename__ = "token_risk_snapshots"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    mint: Mapped[str] = mapped_column(String(44), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (UniqueConstraint("mint", "observed_at", "provider"),)


class PoolSnapshotRecord(Base):
    __tablename__ = "pool_snapshots"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    pool_id: Mapped[UUID] = mapped_column(ForeignKey("pools.id"), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tvl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("pool_id", "observed_at"),
        CheckConstraint("tvl >= 0 AND volume >= 0 AND fees >= 0 AND price >= 0"),
    )


class FeatureVectorRecord(Base):
    __tablename__ = "feature_vectors"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    pool_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("pool_snapshots.id"), nullable=False, index=True
    )
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("pool_snapshot_id", "ruleset_version"),)


class CandidateDecisionRecord(Base):
    __tablename__ = "candidate_decisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    pool_id: Mapped[UUID] = mapped_column(ForeignKey("pools.id"), nullable=False, index=True)
    feature_vector_id: Mapped[UUID] = mapped_column(
        ForeignKey("feature_vectors.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    missing_signals: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="SHADOW")
    config_version: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


class CandidateDecisionHeadRecord(Base):
    """Per pool: the decision that stands and the last cycle that confirmed it (0004)."""

    __tablename__ = "candidate_decision_heads"

    pool_id: Mapped[UUID] = mapped_column(ForeignKey("pools.id"), primary_key=True)
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_decisions.id"), nullable=False, index=True
    )
    outcome_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # The cycle before ``evaluated_at``: the journal finds the last complete cycle with it.
    previous_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyPlanRecord(Base):
    __tablename__ = "strategy_plans"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_decisions.id"), nullable=False, index=True
    )
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    pool_id: Mapped[UUID] = mapped_column(ForeignKey("pools.id"), nullable=False, index=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(78, 0), nullable=False)
    quote_amount: Mapped[Decimal] = mapped_column(Numeric(78, 0), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("base_amount >= 0 AND quote_amount >= 0"),
        CheckConstraint("version >= 1"),
    )


class ExecutionCommandRecord(Base):
    __tablename__ = "execution_commands"

    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("strategy_plans.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("mode IN ('SHADOW', 'APPROVAL', 'LIVE')"),)


class ExecutionReceiptRecord(Base):
    __tablename__ = "execution_receipts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_commands.command_id"), nullable=False, index=True
    )
    signature: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModeTransitionRecord(Base):
    __tablename__ = "mode_transitions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    from_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    to_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    promotion_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operator_confirmation_id: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    evidence_id: Mapped[str | None] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)

    __table_args__ = (
        CheckConstraint("from_mode IN ('SHADOW', 'APPROVAL', 'LIVE')"),
        CheckConstraint("to_mode IN ('SHADOW', 'APPROVAL', 'LIVE')"),
    )


class RiskDayRecord(Base):
    __tablename__ = "risk_days"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    risk_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    starting_equity: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    daily_stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertRecord(Base):
    __tablename__ = "alerts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    code: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    ruleset_version: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


class SystemLeaseRecord(Base):
    __tablename__ = "system_leases"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = (CheckConstraint("expires_at > acquired_at"),)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    causation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)

    __table_args__ = (CheckConstraint("schema_version >= 1"),)


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    causation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("schema_version >= 1"),
        CheckConstraint("publish_attempts >= 0"),
        Index("ix_outbox_unpublished", "occurred_at", postgresql_where=published_at.is_(None)),
    )


# --- T07: mode, risk-day, entry and kill-switch state (D01) -------------------------


class ModeStateRecord(Base):
    __tablename__ = "mode_state"

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    promotion_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # The policy for open positions chosen at activation; NULL while inactive (0003).
    kill_switch_policy: Mapped[str | None] = mapped_column(Text)
    daily_stop_date: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModeApprovalRecord(Base):
    __tablename__ = "mode_approvals"

    approval_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    simulation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortfolioEquityRecord(Base):
    __tablename__ = "portfolio_equity"

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(128), nullable=False)


class EntryPlanRecord(Base):
    __tablename__ = "entry_plans"

    plan_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    position_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


class OpenPositionRecord(Base):
    __tablename__ = "open_positions"

    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


class PromotionObservationRecord(Base):
    __tablename__ = "promotion_observations"

    risk_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ruleset_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PromotionEvidenceRecord(Base):
    __tablename__ = "promotion_evidence"

    evidence_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class LivePromotionClaimRecord(Base):
    __tablename__ = "live_promotion_claims"

    promotion_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KillPositionActionRecord(Base):
    __tablename__ = "kill_position_actions"

    action_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    alert_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    position_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


# --- T07: shadow accounting (D01) ----------------------------------------------------


class ShadowReceiptRecord(Base):
    __tablename__ = "shadow_receipts"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


class ShadowPositionRecord(Base):
    __tablename__ = "shadow_positions"

    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (CheckConstraint("version >= 1"),)


class ShadowLedgerRecord(Base):
    __tablename__ = "shadow_ledger"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


class PositionOutcomeRecord(Base):
    __tablename__ = "position_outcomes"

    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)


# --- T07: execution store, universe history and position context -------------------


class ExecutionStateRecord(Base):
    __tablename__ = "execution_records"

    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionApprovalClaimRecord(Base):
    __tablename__ = "execution_approval_claims"

    approval_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    simulation_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class UniverseSyncRecord(Base):
    __tablename__ = "universe_syncs"

    sync_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    pool_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UniverseMemberRecord(Base):
    __tablename__ = "universe_members"

    sync_id: Mapped[UUID] = mapped_column(
        ForeignKey("universe_syncs.sync_id"), primary_key=True
    )
    pool_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("pool_snapshots.id"), primary_key=True
    )


class PositionContextRecord(Base):
    __tablename__ = "position_contexts"

    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    pool_address: Mapped[str] = mapped_column(String(44), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
