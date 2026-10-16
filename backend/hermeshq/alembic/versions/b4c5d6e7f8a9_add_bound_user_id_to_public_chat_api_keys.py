"""add bound_user_id to public_chat_api_keys

Revision ID: b4c5d6e7f8a9
Revises: bb22cc33dd44
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "bb22cc33dd44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "public_chat_api_keys",
        sa.Column("bound_user_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_public_chat_api_keys_bound_user_id_users",
        "public_chat_api_keys",
        "users",
        ["bound_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_public_chat_api_keys_bound_user_id", "public_chat_api_keys", ["bound_user_id"])
    op.add_column(
        "public_chat_api_keys",
        sa.Column("trust_sender_id", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("public_chat_sessions", sa.Column("sender_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("public_chat_sessions", "sender_id")
    op.drop_column("public_chat_api_keys", "trust_sender_id")
    op.drop_index("ix_public_chat_api_keys_bound_user_id", table_name="public_chat_api_keys")
    op.drop_constraint("fk_public_chat_api_keys_bound_user_id_users", "public_chat_api_keys", type_="foreignkey")
    op.drop_column("public_chat_api_keys", "bound_user_id")
