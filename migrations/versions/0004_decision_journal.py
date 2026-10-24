"""Decision heads, journal indexes and the indexes telemetry retention needs (R29i.2).

* ``candidate_decision_heads`` - per pool the decision that stands, the last cycle that
  confirmed it and the cycle before: an unchanged outcome only moves ``evaluated_at``
  instead of appending a new decision record every cycle, and the panel reads the
  outcomes of the current cycles from here.
  Existing data gets one head per pool (its newest decision, an empty outcome key, so the
  next evaluation records afresh).
* ``candidate_decisions (seq)`` and ``(pool_id, seq DESC)`` - newest-first pages, overall
  and per pool, without sorting the whole table.
* ``candidate_decisions (feature_vector_id)``, ``universe_members (pool_snapshot_id)`` and
  ``pool_snapshots (observed_at)`` - retention deletes by age, and each delete checks the
  foreign keys pointing at the row.

Revision ID: 0004_decision_journal
Revises: 0003_kill_switch_policy
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_decision_journal"
down_revision: str | None = "0003_kill_switch_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_index("ix_candidate_decisions_seq", "candidate_decisions", ["seq"], unique=True)
    op.create_index(
        "ix_candidate_decisions_pool_seq",
        "candidate_decisions",
        ["pool_id", sa.text("seq DESC")],
    )
    op.create_index(
        "ix_candidate_decisions_feature_vector_id", "candidate_decisions", ["feature_vector_id"]
    )
    op.create_index(
        "ix_universe_members_pool_snapshot_id", "universe_members", ["pool_snapshot_id"]
    )
    op.create_index("ix_pool_snapshots_observed_at", "pool_snapshots", ["observed_at"])
    op.create_table(
        "candidate_decision_heads",
        sa.Column("pool_id", UUID, nullable=False),
        sa.Column("decision_id", UUID, nullable=False),
        sa.Column("outcome_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reasons", JSON, nullable=False),
        sa.Column("decided_at", TIMESTAMP, nullable=False),
        sa.Column("evaluated_at", TIMESTAMP, nullable=False),
        sa.Column("previous_evaluated_at", TIMESTAMP),
        sa.ForeignKeyConstraint(["pool_id"], ["pools.id"], name="fk_candidate_decision_heads_pool"),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["candidate_decisions.id"],
            name="fk_candidate_decision_heads_decision",
        ),
        sa.PrimaryKeyConstraint("pool_id", name="pk_candidate_decision_heads"),
    )
    op.create_index(
        "ix_candidate_decision_heads_decision_id", "candidate_decision_heads", ["decision_id"]
    )
    op.create_index(
        "ix_candidate_decision_heads_evaluated_at", "candidate_decision_heads", ["evaluated_at"]
    )
    op.execute(
        """
        INSERT INTO candidate_decision_heads
            (pool_id, decision_id, outcome_key, status, reasons, decided_at, evaluated_at)
        SELECT DISTINCT ON (pool_id) pool_id, id, '', status, reasons, decided_at, decided_at
        FROM candidate_decisions
        ORDER BY pool_id, seq DESC
        """
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_decision_heads_evaluated_at", table_name="candidate_decision_heads")
    op.drop_index("ix_candidate_decision_heads_decision_id", table_name="candidate_decision_heads")
    op.drop_table("candidate_decision_heads")
    op.drop_index("ix_pool_snapshots_observed_at", table_name="pool_snapshots")
    op.drop_index("ix_universe_members_pool_snapshot_id", table_name="universe_members")
    op.drop_index("ix_candidate_decisions_feature_vector_id", table_name="candidate_decisions")
    op.drop_index("ix_candidate_decisions_pool_seq", table_name="candidate_decisions")
    op.drop_index("ix_candidate_decisions_seq", table_name="candidate_decisions")
