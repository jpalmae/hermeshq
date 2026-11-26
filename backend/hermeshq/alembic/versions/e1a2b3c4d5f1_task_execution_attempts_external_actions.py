from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f1"
down_revision: str | Sequence[str] | None = "c9f4a7e1b320"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_execution_attempts",
        sa.Column("attempt_id", sa.String(length=36), primary_key=True),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runner_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_count", sa.Integer(), nullable=False),
        sa.Column("runner_heartbeat_seq", sa.BigInteger(), nullable=False),
        sa.Column("tool_events", sa.Integer(), nullable=False),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        sa.Column("reconciliation", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'lost', 'abandoned')",
            name="ck_task_execution_attempts_state",
        ),
    )
    op.create_index("ix_task_execution_attempts_execution_id", "task_execution_attempts", ["execution_id"])
    op.create_index("ix_task_execution_attempts_task_id", "task_execution_attempts", ["task_id"])
    op.create_index(
        "uq_task_execution_attempts_live_task",
        "task_execution_attempts",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'running')"),
    )
    op.create_table(
        "external_action_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=160), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('iniciada', 'confirmada', 'no_ejecutada', 'conciliada_ejecutada', 'conciliada_no_ejecutada')",
            name="ck_external_action_records_state",
        ),
    )
    op.create_index("ix_external_action_records_task_id", "external_action_records", ["task_id"])
    op.create_index("ix_external_action_records_attempt_id", "external_action_records", ["attempt_id"])
    op.create_index("ix_external_action_records_fingerprint", "external_action_records", ["fingerprint"])


def downgrade() -> None:
    op.drop_table("external_action_records")
    op.drop_table("task_execution_attempts")
