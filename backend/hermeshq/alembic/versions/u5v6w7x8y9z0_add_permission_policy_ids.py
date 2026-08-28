"""add permission_policy_ids for policy chaining

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u5v6w7x8y9z0"
down_revision: str | Sequence[str] | None = "t4u5v6w7x8y9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("permission_policy_ids", sa.JSON, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("agents", "permission_policy_ids")
