"""add capability_requests

Revision ID: c9f4a7e1b320
Revises: a9b0c1d2e3f4
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f4a7e1b320"
down_revision: str | Sequence[str] | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "requested_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("example", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Text(), nullable=True),
        sa.Column("files_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="nuevo"),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column("me_too_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_capability_requests_agent_id", "capability_requests", ["agent_id"])
    op.create_index("ix_capability_requests_requested_by_user_id", "capability_requests", ["requested_by_user_id"])
    op.create_index("ix_capability_requests_status", "capability_requests", ["status"])
    op.create_index("ix_capability_requests_fingerprint", "capability_requests", ["fingerprint"])


def downgrade() -> None:
    op.drop_table("capability_requests")
