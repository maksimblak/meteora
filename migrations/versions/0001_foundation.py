"""Create durable state, audit and outbox tables.

Revision ID: 0001_foundation
Revises: None
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "pools",
        sa.Column("id", UUID, nullable=False),
        sa.Column("address", sa.String(44), nullable=False),
        sa.Column("base_mint", sa.String(44), nullable=False),
        sa.Column("quote_mint", sa.String(44), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pools"),
        sa.UniqueConstraint("address", name="uq_pools_address"),
    )
    op.create_table(
        "token_risk_snapshots",
        sa.Column("id", UUID, nullable=False),
        sa.Column("mint", sa.String(44), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("data_quality", sa.String(32), nullable=False),
        sa.Column("snapshot", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_token_risk_snapshots"),
        sa.UniqueConstraint(
            "mint", "observed_at", "provider", name="uq_token_risk_snapshot_observation"
        ),
    )
    op.create_index("ix_token_risk_snapshots_mint", "token_risk_snapshots", ["mint"])
    op.create_table(
        "pool_snapshots",
        sa.Column("id", UUID, nullable=False),
        sa.Column("pool_id", UUID, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tvl", sa.Numeric(38, 18), nullable=False),
        sa.Column("volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("fees", sa.Numeric(38, 18), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("data_quality", sa.String(32), nullable=False),
        sa.Column("raw_snapshot", JSON, nullable=False),
        sa.CheckConstraint(
            "tvl >= 0 AND volume >= 0 AND fees >= 0 AND price >= 0",
            name="ck_pool_snapshots_nonnegative_values",
        ),
        sa.ForeignKeyConstraint(["pool_id"], ["pools.id"], name="fk_pool_snapshots_pool"),
        sa.PrimaryKeyConstraint("id", name="pk_pool_snapshots"),
        sa.UniqueConstraint("pool_id", "observed_at", name="uq_pool_snapshot_observation"),
    )
    op.create_index("ix_pool_snapshots_pool_id", "pool_snapshots", ["pool_id"])
    op.create_table(
        "feature_vectors",
        sa.Column("id", UUID, nullable=False),
        sa.Column("pool_snapshot_id", UUID, nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("values", JSON, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["pool_snapshot_id"], ["pool_snapshots.id"], name="fk_feature_vectors_snapshot"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feature_vectors"),
        sa.UniqueConstraint(
            "pool_snapshot_id", "ruleset_version", name="uq_feature_vector_ruleset"
        ),
    )
    op.create_index("ix_feature_vectors_pool_snapshot_id", "feature_vectors", ["pool_snapshot_id"])
    op.create_table(
        "candidate_decisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("pool_id", UUID, nullable=False),
        sa.Column("feature_vector_id", UUID, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("score", sa.Numeric(20, 10), nullable=False),
        sa.Column("reasons", JSON, nullable=False),
        sa.Column("missing_signals", JSON, nullable=False),
        sa.Column("evidence", JSON, nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], ["pools.id"], name="fk_candidate_decisions_pool"),
        sa.ForeignKeyConstraint(
            ["feature_vector_id"], ["feature_vectors.id"], name="fk_candidate_decisions_features"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_decisions"),
    )
    op.create_index("ix_candidate_decisions_pool_id", "candidate_decisions", ["pool_id"])
    op.create_index(
        "ix_candidate_decisions_correlation_id", "candidate_decisions", ["correlation_id"]
    )
    op.create_table(
        "strategy_plans",
        sa.Column("id", UUID, nullable=False),
        sa.Column("decision_id", UUID, nullable=False),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("parameters", JSON, nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["candidate_decisions.id"], name="fk_strategy_plans_decision"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_plans"),
    )
    op.create_index("ix_strategy_plans_decision_id", "strategy_plans", ["decision_id"])
    op.create_index("ix_strategy_plans_correlation_id", "strategy_plans", ["correlation_id"])
    op.create_table(
        "positions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("pool_id", UUID, nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("base_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("quote_amount", sa.Numeric(78, 0), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "base_amount >= 0 AND quote_amount >= 0", name="ck_positions_nonnegative_amounts"
        ),
        sa.CheckConstraint("version >= 1", name="ck_positions_positive_version"),
        sa.ForeignKeyConstraint(["pool_id"], ["pools.id"], name="fk_positions_pool"),
        sa.ForeignKeyConstraint(["plan_id"], ["strategy_plans.id"], name="fk_positions_plan"),
        sa.PrimaryKeyConstraint("id", name="pk_positions"),
    )
    op.create_index("ix_positions_pool_id", "positions", ["pool_id"])
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_table(
        "execution_commands",
        sa.Column("command_id", UUID, nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('SHADOW', 'APPROVAL', 'LIVE')", name="ck_execution_commands_mode"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["strategy_plans.id"], name="fk_execution_commands_plan"
        ),
        sa.PrimaryKeyConstraint("command_id", name="pk_execution_commands"),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_commands_idempotency_key"),
    )
    op.create_index(
        "ix_execution_commands_correlation_id", "execution_commands", ["correlation_id"]
    )
    op.create_index("ix_execution_commands_status", "execution_commands", ["status"])
    op.create_table(
        "execution_receipts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("command_id", UUID, nullable=False),
        sa.Column("signature", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("receipt", JSON, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["command_id"], ["execution_commands.command_id"], name="fk_execution_receipts_command"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_receipts"),
        sa.UniqueConstraint("signature", name="uq_execution_receipts_signature"),
    )
    op.create_index("ix_execution_receipts_command_id", "execution_receipts", ["command_id"])
    op.create_table(
        "mode_transitions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("from_mode", sa.String(16), nullable=False),
        sa.Column("to_mode", sa.String(16), nullable=False),
        sa.Column("promotion_authorized", sa.Boolean(), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_mode IN ('SHADOW', 'APPROVAL', 'LIVE')", name="ck_mode_transitions_from_mode"
        ),
        sa.CheckConstraint(
            "to_mode IN ('SHADOW', 'APPROVAL', 'LIVE')", name="ck_mode_transitions_to_mode"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mode_transitions"),
    )
    op.create_index("ix_mode_transitions_correlation_id", "mode_transitions", ["correlation_id"])
    op.create_table(
        "risk_days",
        sa.Column("id", UUID, nullable=False),
        sa.Column("risk_date", sa.Date(), nullable=False),
        sa.Column("starting_equity", sa.Numeric(38, 18), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(38, 18), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(38, 18), nullable=False),
        sa.Column("daily_stop_triggered", sa.Boolean(), nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_risk_days"),
        sa.UniqueConstraint("risk_date", name="uq_risk_days_risk_date"),
    )
    op.create_table(
        "alerts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
    )
    op.create_index("ix_alerts_severity", "alerts", ["severity"])
    op.create_index("ix_alerts_status", "alerts", ["status"])
    op.create_index("ix_alerts_correlation_id", "alerts", ["correlation_id"])
    op.create_table(
        "system_leases",
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > acquired_at", name="ck_system_leases_expiry"),
        sa.PrimaryKeyConstraint("name", name="pk_system_leases"),
    )
    op.create_index("ix_system_leases_expires_at", "system_leases", ["expires_at"])
    op.create_table(
        "audit_events",
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("causation_id", UUID),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.CheckConstraint("schema_version >= 1", name="ck_audit_events_schema_version"),
        sa.PrimaryKeyConstraint("event_id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_aggregate_id", "audit_events", ["aggregate_id"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_table(
        "outbox_events",
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("causation_id", UUID),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("schema_version >= 1", name="ck_outbox_events_schema_version"),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_outbox_events_publish_attempts"),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_events"),
    )
    op.create_index("ix_outbox_events_correlation_id", "outbox_events", ["correlation_id"])
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_published_at", "outbox_events", ["published_at"])
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_events",
        ["occurred_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    for table_name in (
        "outbox_events",
        "audit_events",
        "system_leases",
        "alerts",
        "risk_days",
        "mode_transitions",
        "execution_receipts",
        "execution_commands",
        "positions",
        "strategy_plans",
        "candidate_decisions",
        "feature_vectors",
        "pool_snapshots",
        "token_risk_snapshots",
        "pools",
    ):
        op.drop_table(table_name)
