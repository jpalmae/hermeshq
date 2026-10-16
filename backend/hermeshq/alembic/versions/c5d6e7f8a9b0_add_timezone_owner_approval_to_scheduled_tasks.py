"""add timezone, owner and approval to scheduled_tasks

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"))
    op.add_column("scheduled_tasks", sa.Column("owner_user_id", sa.String(36), nullable=True))
    op.add_column(
        "scheduled_tasks",
        sa.Column("approval_status", sa.String(16), nullable=False, server_default="approved"),
    )
    op.add_column("scheduled_tasks", sa.Column("created_by_agent_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_scheduled_tasks_owner_user_id_users",
        "scheduled_tasks",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_created_by_agent_id_agents",
        "scheduled_tasks",
        "agents",
        ["created_by_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_scheduled_tasks_owner_user_id", "scheduled_tasks", ["owner_user_id"])
    op.create_index("ix_scheduled_tasks_created_by_agent_id", "scheduled_tasks", ["created_by_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_created_by_agent_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_owner_user_id", table_name="scheduled_tasks")
    op.drop_constraint("fk_scheduled_tasks_created_by_agent_id_agents", "scheduled_tasks", type_="foreignkey")
    op.drop_constraint("fk_scheduled_tasks_owner_user_id_users", "scheduled_tasks", type_="foreignkey")
    op.drop_column("scheduled_tasks", "created_by_agent_id")
    op.drop_column("scheduled_tasks", "approval_status")
    op.drop_column("scheduled_tasks", "owner_user_id")
    op.drop_column("scheduled_tasks", "timezone")
