"""add oidc_providers table

Revision ID: h2i3j4k5l6m7
Revises: e2f3a4b5c6d7
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "h2i3j4k5l6m7"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    if not _table_exists("oidc_providers"):
        op.create_table(
            "oidc_providers",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("slug", sa.String(64), unique=True, index=True, nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("client_id", sa.String(512), nullable=False),
            sa.Column("client_secret", sa.String(512), nullable=False),
            sa.Column("discovery_url", sa.String(1024), nullable=False),
            sa.Column("scopes", sa.String(512), nullable=False, server_default="openid profile email"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("allowed_domains", sa.Text(), nullable=True),
            sa.Column("icon_slug", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    pass
