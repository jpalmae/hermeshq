from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f2"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("execution_lane", sa.String(length=16), server_default="default", nullable=False),
    )
    op.drop_index("uq_tasks_one_running_per_agent", table_name="tasks")
    op.create_index(
        "uq_tasks_one_running_per_agent",
        "tasks",
        ["agent_id", "execution_lane"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_one_running_per_agent", table_name="tasks")
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET status = 'queued', claimed_by = NULL, claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL
            WHERE status = 'running' AND execution_lane <> 'default'
            """
        )
    )
    op.create_index(
        "uq_tasks_one_running_per_agent",
        "tasks",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_column("tasks", "execution_lane")
