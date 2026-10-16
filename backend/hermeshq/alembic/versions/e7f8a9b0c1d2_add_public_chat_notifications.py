"""add public_chat_notifications and scheduled_tasks.notify_public_chat

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("notify_public_chat", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE scheduled_tasks SET notify_public_chat = true WHERE created_by_agent_id IS NOT NULL")
    op.create_table(
        "public_chat_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "api_key_id", sa.String(36), sa.ForeignKey("public_chat_api_keys.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sender_id", sa.String(255), nullable=True),
        sa.Column("schedule_id", sa.String(36), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_public_chat_notifications_key_status", "public_chat_notifications", ["api_key_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_public_chat_notifications_key_status", table_name="public_chat_notifications")
    op.drop_table("public_chat_notifications")
    op.drop_column("scheduled_tasks", "notify_public_chat")
