"""Durable mode, accounting, execution, universe and worker coordination state.

Moves the T04 mode/accounting stores and the T05 execution store onto the shared
PostgreSQL database (D01), makes the audit trail append-only and adds publish and
lease bookkeeping columns to the T01 outbox and lease tables.

Revision ID: 0002_worker_state
Revises: 0001_foundation
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_worker_state"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMP = sa.DateTime(timezone=True)

_NEW_TABLES = (
    "position_contexts",
    "universe_members",
    "universe_syncs",
    "execution_approval_claims",
    "execution_records",
    "position_outcomes",
    "shadow_ledger",
    "shadow_positions",
    "shadow_receipts",
    "kill_position_actions",
    "live_promotion_claims",
    "promotion_evidence",
    "promotion_observations",
    "open_positions",
    "entry_plans",
    "portfolio_equity",
    "mode_approvals",
    "mode_state",
)


def _seq() -> sa.Column[int]:
    return sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False)


def upgrade() -> None:
    # --- T01 audit trail: append-only, ordered -------------------------------------
    op.add_column("audit_events", _seq())
    op.create_index("ix_audit_events_seq", "audit_events", ["seq"], unique=True)
    op.execute(
        """
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation()"
    )

    # --- T01 outbox: publish order, retry backoff and relay claims ------------------
    op.add_column("outbox_events", _seq())
    op.add_column("outbox_events", sa.Column("next_attempt_at", TIMESTAMP))
    op.add_column("outbox_events", sa.Column("claimed_by", sa.String(128)))
    op.add_column("outbox_events", sa.Column("claimed_until", TIMESTAMP))
    op.add_column("outbox_events", sa.Column("last_error", sa.Text()))
    op.create_index("ix_outbox_events_seq", "outbox_events", ["seq"], unique=True)
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # --- T01 leases: monotonic fencing token ------------------------------------------
    op.add_column(
        "system_leases",
        sa.Column("token", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute("CREATE SEQUENCE system_lease_tokens AS BIGINT")

    # --- mode state and transitions ------------------------------------------------------
    op.create_table(
        "mode_state",
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("promotion_authorized", sa.Boolean(), nullable=False),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False),
        sa.Column("daily_stop_date", sa.Date()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("singleton = 1", name="ck_mode_state_singleton"),
        sa.CheckConstraint("mode IN ('SHADOW', 'APPROVAL', 'LIVE')", name="ck_mode_state_mode"),
        sa.CheckConstraint("version >= 1", name="ck_mode_state_version"),
        sa.PrimaryKeyConstraint("singleton", name="pk_mode_state"),
    )
    op.add_column(
        "mode_transitions",
        sa.Column("operator_confirmation_id", sa.String(128), nullable=False, server_default=""),
    )
    op.add_column(
        "mode_transitions", sa.Column("reason", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("mode_transitions", sa.Column("evidence_id", sa.String(64)))
    op.add_column("mode_transitions", _seq())
    op.create_table(
        "mode_approvals",
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("command_id", UUID, nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("simulation_digest", sa.String(64), nullable=False),
        sa.Column("operator_id", sa.String(128), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("issued_at", TIMESTAMP, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("consumed_at", TIMESTAMP),
        sa.CheckConstraint("expires_at > issued_at", name="ck_mode_approvals_ttl"),
        sa.PrimaryKeyConstraint("approval_id", name="pk_mode_approvals"),
    )
    op.create_index("ix_mode_approvals_command_id", "mode_approvals", ["command_id"])

    # --- risk days: exact decimals and the start-of-day baseline -------------------
    for column in ("starting_equity", "realized_pnl", "unrealized_pnl"):
        op.alter_column("risk_days", column, type_=sa.Numeric(), existing_nullable=False)
    op.add_column(
        "risk_days",
        sa.Column("baseline_observed_at", TIMESTAMP, nullable=False, server_default=sa.func.now()),
    )
    op.add_column("risk_days", sa.Column("triggered_at", TIMESTAMP))
    op.add_column(
        "risk_days",
        sa.Column("updated_at", TIMESTAMP, nullable=False, server_default=sa.func.now()),
    )
    op.alter_column("risk_days", "baseline_observed_at", server_default=None)
    op.alter_column("risk_days", "updated_at", server_default=None)
    op.create_check_constraint(
        "ck_risk_days_trigger_consistency",
        "risk_days",
        "daily_stop_triggered = (triggered_at IS NOT NULL)",
    )
    op.create_table(
        "portfolio_equity",
        sa.Column("observed_at", TIMESTAMP, nullable=False),
        sa.Column("equity", sa.Numeric(), nullable=False),
        sa.Column("ruleset_version", sa.String(128), nullable=False),
        sa.CheckConstraint("equity > 0", name="ck_portfolio_equity_positive"),
        sa.PrimaryKeyConstraint("observed_at", name="pk_portfolio_equity"),
    )

    # --- entries, open positions, alerts and kill-switch actions --------------------
    op.create_table(
        "entry_plans",
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("admitted_at", TIMESTAMP, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("position_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        _seq(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'EXECUTING', 'OPENED', 'FAILED', 'CANCELLED')",
            name="ck_entry_plans_status",
        ),
        sa.CheckConstraint("expires_at > admitted_at", name="ck_entry_plans_ttl"),
        sa.PrimaryKeyConstraint("plan_id", name="pk_entry_plans"),
    )
    op.create_index("ix_entry_plans_status", "entry_plans", ["status"])
    op.create_table(
        "open_positions",
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("position_id", name="pk_open_positions"),
    )
    op.add_column("alerts", sa.Column("code", sa.String(64), nullable=False, server_default=""))
    op.add_column(
        "alerts", sa.Column("ruleset_version", sa.String(128), nullable=False, server_default="")
    )
    op.add_column("alerts", sa.Column("resolution", sa.Text(), nullable=False, server_default=""))
    op.add_column("alerts", _seq())
    op.create_table(
        "kill_position_actions",
        sa.Column("action_key", sa.String(256), nullable=False),
        sa.Column("alert_id", UUID, nullable=False),
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        _seq(),
        sa.PrimaryKeyConstraint("action_key", name="pk_kill_position_actions"),
    )
    op.create_index("ix_kill_position_actions_alert_id", "kill_position_actions", ["alert_id"])

    # --- promotion evidence ------------------------------------------------------------
    op.create_table(
        "promotion_observations",
        sa.Column("risk_date", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.PrimaryKeyConstraint(
            "risk_date", "ruleset_version", "mode", name="pk_promotion_observations"
        ),
    )
    op.create_table(
        "promotion_evidence",
        sa.Column("evidence_id", UUID, nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_promotion_evidence"),
    )
    op.create_table(
        "live_promotion_claims",
        sa.Column("promotion_id", UUID, nullable=False),
        sa.Column("claimed_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("promotion_id", name="pk_live_promotion_claims"),
    )

    # --- shadow accounting -------------------------------------------------------------
    op.create_table(
        "shadow_receipts",
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("command_id", UUID, nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        _seq(),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_shadow_receipts"),
        sa.UniqueConstraint("command_id", name="uq_shadow_receipts_command_id"),
    )
    op.create_table(
        "shadow_positions",
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_shadow_positions_version"),
        sa.PrimaryKeyConstraint("position_id", name="pk_shadow_positions"),
    )
    op.create_index("ix_shadow_positions_status", "shadow_positions", ["status"])
    op.create_table(
        "shadow_ledger",
        sa.Column("entry_id", sa.String(128), nullable=False),
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        _seq(),
        sa.PrimaryKeyConstraint("entry_id", name="pk_shadow_ledger"),
    )
    op.create_index("ix_shadow_ledger_position_id", "shadow_ledger", ["position_id"])
    op.create_table(
        "position_outcomes",
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("closed_at", TIMESTAMP, nullable=False),
        sa.Column("payload", JSON, nullable=False),
        _seq(),
        sa.PrimaryKeyConstraint("position_id", name="pk_position_outcomes"),
    )
    op.create_index("ix_position_outcomes_closed_at", "position_outcomes", ["closed_at"])

    # --- execution store ----------------------------------------------------------------
    op.create_table(
        "execution_records",
        sa.Column("command_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("command_id", name="pk_execution_records"),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_records_idempotency_key"),
    )
    op.create_index("ix_execution_records_phase", "execution_records", ["phase"])
    op.create_table(
        "execution_approval_claims",
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("simulation_digest", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("approval_id", name="pk_execution_approval_claims"),
    )

    # --- universe versions, decision records and position context -------------------
    op.create_table(
        "universe_syncs",
        sa.Column("sync_id", UUID, nullable=False),
        sa.Column("observed_at", TIMESTAMP, nullable=False),
        sa.Column("pool_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("committed_at", TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("sync_id", name="pk_universe_syncs"),
    )
    op.create_index("ix_universe_syncs_observed_at", "universe_syncs", ["observed_at"])
    op.create_table(
        "universe_members",
        sa.Column("sync_id", UUID, nullable=False),
        sa.Column("pool_snapshot_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["sync_id"], ["universe_syncs.sync_id"], name="fk_universe_members_sync"
        ),
        sa.ForeignKeyConstraint(
            ["pool_snapshot_id"], ["pool_snapshots.id"], name="fk_universe_members_snapshot"
        ),
        sa.PrimaryKeyConstraint("sync_id", "pool_snapshot_id", name="pk_universe_members"),
    )
    op.alter_column("candidate_decisions", "score", existing_type=sa.Numeric(20, 10), nullable=True)
    op.add_column(
        "candidate_decisions",
        sa.Column("mode", sa.String(16), nullable=False, server_default="SHADOW"),
    )
    op.add_column(
        "candidate_decisions",
        sa.Column("config_version", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column("candidate_decisions", _seq())
    op.create_index("ix_candidate_decisions_decided_at", "candidate_decisions", ["decided_at"])
    op.create_index("ix_candidate_decisions_status", "candidate_decisions", ["status"])
    op.create_table(
        "position_contexts",
        sa.Column("position_id", sa.String(128), nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("pool_address", sa.String(44), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.PrimaryKeyConstraint("position_id", name="pk_position_contexts"),
        sa.UniqueConstraint("plan_id", name="uq_position_contexts_plan_id"),
    )
    op.create_index("ix_position_contexts_pool_address", "position_contexts", ["pool_address"])


def downgrade() -> None:
    op.drop_index("ix_candidate_decisions_status", table_name="candidate_decisions")
    op.drop_index("ix_candidate_decisions_decided_at", table_name="candidate_decisions")
    for column in ("seq", "config_version", "mode"):
        op.drop_column("candidate_decisions", column)
    # ``score`` stays nullable: unscored decision records are audit data and are never
    # deleted or back-filled with an invented score by a downgrade.
    for table_name in _NEW_TABLES:
        op.drop_table(table_name)
    for column in ("seq", "resolution", "ruleset_version", "code"):
        op.drop_column("alerts", column)
    op.drop_constraint("ck_risk_days_trigger_consistency", "risk_days", type_="check")
    for column in ("updated_at", "triggered_at", "baseline_observed_at"):
        op.drop_column("risk_days", column)
    for column in ("starting_equity", "realized_pnl", "unrealized_pnl"):
        op.alter_column("risk_days", column, type_=sa.Numeric(38, 18), existing_nullable=False)
    for column in ("seq", "evidence_id", "reason", "operator_confirmation_id"):
        op.drop_column("mode_transitions", column)
    op.execute("DROP SEQUENCE system_lease_tokens")
    op.drop_column("system_leases", "token")
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_index("ix_outbox_events_seq", table_name="outbox_events")
    for column in ("last_error", "claimed_until", "claimed_by", "next_attempt_at", "seq"):
        op.drop_column("outbox_events", column)
    op.execute("DROP TRIGGER audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION reject_audit_mutation()")
    op.drop_index("ix_audit_events_seq", table_name="audit_events")
    op.drop_column("audit_events", "seq")
