"""The kill-switch policy chosen at activation is part of the durable mode state.

Spec 7.3: open positions are held (``HOLD_AND_ALERT``) or exited in an orderly way
(``ORDERLY_EXIT``) as the operator chose when activating the kill switch; the worker,
the execution service and the approval desk read that choice back from ``mode_state``.
It is set only while the switch is active and cleared on release.

Revision ID: 0003_kill_switch_policy
Revises: 0002_worker_state
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_kill_switch_policy"
down_revision: str | None = "0002_worker_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mode_state", sa.Column("kill_switch_policy", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_mode_state_kill_switch_policy",
        "mode_state",
        "kill_switch_policy IS NULL OR (kill_switch_active AND kill_switch_policy IN "
        "('HOLD_AND_ALERT', 'ORDERLY_EXIT'))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_mode_state_kill_switch_policy", "mode_state", type_="check")
    op.drop_column("mode_state", "kill_switch_policy")
