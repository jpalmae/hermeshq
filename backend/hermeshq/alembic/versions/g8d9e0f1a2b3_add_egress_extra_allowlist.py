"""Add egress_extra_allowlist to app_settings for auto-allowed provider endpoints

Revision ID: g8d9e0f1a2b3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("egress_extra_allowlist", sa.JSON, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "egress_extra_allowlist")
