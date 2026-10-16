"""add approval_code to scheduled_tasks

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("approval_code", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "approval_code")
