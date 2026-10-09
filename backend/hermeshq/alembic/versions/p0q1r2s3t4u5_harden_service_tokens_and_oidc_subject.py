"""harden service tokens and oidc subject

Revision ID: p0q1r2s3t4u5
Revises: o9p0q1r2s3t4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p0q1r2s3t4u5"
down_revision: str | Sequence[str] | None = "o9p0q1r2s3t4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("service_token_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("oidc_providers", "client_secret", existing_type=sa.String(length=512), type_=sa.Text())
    op.alter_column(
        "users",
        "oidc_subject",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
    )
    op.drop_index("ix_users_oidc_subject", table_name="users", if_exists=True)
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_oidc_subject", table_name="users")
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"], unique=False)
    op.alter_column(
        "users",
        "oidc_subject",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
    )
    op.alter_column("oidc_providers", "client_secret", existing_type=sa.Text(), type_=sa.String(length=512))
    op.drop_column("agents", "service_token_version")
