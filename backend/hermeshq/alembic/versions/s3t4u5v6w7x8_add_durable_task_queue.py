"""Add durable task claims and per-agent serialization.

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s3t4u5v6w7x8"
down_revision: str | Sequence[str] | None = "r2s3t4u5v6w7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("tasks", sa.Column("claimed_by", sa.String(length=36), nullable=True))
    op.add_column("tasks", sa.Column("claim_token", sa.String(length=36), nullable=True))
    op.add_column("tasks", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_tasks_attempt_count_nonnegative", "tasks", "attempt_count >= 0")
    op.alter_column("tasks", "attempt_count", server_default=None)

    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET status = 'queued',
                board_column = CASE WHEN board_manual THEN board_column ELSE 'inbox' END,
                started_at = NULL,
                completed_at = NULL
            WHERE status = 'running'
            """
        )
    )

    op.create_index(
        "ix_tasks_queue_dispatch",
        "tasks",
        ["agent_id", "status", "queued_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_expired_leases",
        "tasks",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_tasks_one_running_per_agent",
        "tasks",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_one_running_per_agent", table_name="tasks")
    op.drop_index("ix_tasks_expired_leases", table_name="tasks")
    op.drop_index("ix_tasks_queue_dispatch", table_name="tasks")
    op.drop_constraint("ck_tasks_attempt_count_nonnegative", "tasks", type_="check")
    op.drop_column("tasks", "cancel_requested_at")
    op.drop_column("tasks", "lease_expires_at")
    op.drop_column("tasks", "claimed_at")
    op.drop_column("tasks", "claim_token")
    op.drop_column("tasks", "claimed_by")
    op.drop_column("tasks", "attempt_count")
