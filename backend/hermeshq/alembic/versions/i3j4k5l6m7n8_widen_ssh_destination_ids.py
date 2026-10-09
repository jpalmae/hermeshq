"""Widen ssh_destinations id/agent/user columns to fit UUID strings

Revision ID: i3j4k5l6m7n8
Revises: h1i2j3k4l5m6
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "i3j4k5l6m7n8"
down_revision: str | Sequence[str] | None = "h1i2j3k4l5m6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("ssh_destinations", "id", type_=sa.String(36), existing_type=sa.String(32))
    op.alter_column("ssh_destinations", "allowed_agent_id", type_=sa.String(36), existing_type=sa.String(32))
    op.alter_column("ssh_destinations", "created_by_user_id", type_=sa.String(36), existing_type=sa.String(32))


def downgrade() -> None:
    op.alter_column("ssh_destinations", "id", type_=sa.String(32), existing_type=sa.String(36))
    op.alter_column("ssh_destinations", "allowed_agent_id", type_=sa.String(32), existing_type=sa.String(36))
    op.alter_column("ssh_destinations", "created_by_user_id", type_=sa.String(32), existing_type=sa.String(36))
