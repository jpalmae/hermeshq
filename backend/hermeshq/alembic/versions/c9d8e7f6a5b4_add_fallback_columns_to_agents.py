"""add fallback columns to agents

Revision ID: c9d8e7f6a5b4
Revises: a1b2c3d4e5f6
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "c9d8e7f6a5b4"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    columns = [col["name"] for col in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists("agents", "fallback_provider"):
        op.add_column("agents", sa.Column("fallback_provider", sa.String(64), nullable=True))
    if not _column_exists("agents", "fallback_model"):
        op.add_column("agents", sa.Column("fallback_model", sa.String(255), nullable=True))
    if not _column_exists("agents", "fallback_api_key_ref"):
        op.add_column("agents", sa.Column("fallback_api_key_ref", sa.String(128), nullable=True))
    if not _column_exists("agents", "fallback_base_url"):
        op.add_column("agents", sa.Column("fallback_base_url", sa.String(512), nullable=True))


def downgrade() -> None:
    pass
