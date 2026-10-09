"""Add ssh_destinations for scoped operator SSH relay

Revision ID: h1i2j3k4l5m6
Revises: g8d9e0f1a2b3
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h1i2j3k4l5m6"
down_revision: str | Sequence[str] | None = "g8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ssh_destinations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("listen_port", sa.Integer(), nullable=False),
        sa.Column(
            "allowed_agent_id",
            sa.String(32),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.String(512), nullable=True),
        sa.Column("created_by_user_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("host", "port", name="uq_ssh_destination_host_port"),
    )
    op.create_index("ix_ssh_destinations_allowed_agent_id", "ssh_destinations", ["allowed_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_ssh_destinations_allowed_agent_id", table_name="ssh_destinations")
    op.drop_table("ssh_destinations")
