"""repair: add audit_logs table if missing

Revision ID: i3j4k5l6m7n8
Revises: g1h2i3j4k5l6
Create Date: 2026-06-20

Repair migration for deployments where audit_logs was never created
because the d4e5f6a7b8c9 migration was skipped in the applied chain.
All operations are idempotent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "i3j4k5l6m7n8"
down_revision: str | None = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    if "audit_logs" not in existing_tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("actor_username", sa.String(128), nullable=True),
            sa.Column("actor_role", sa.String(20), nullable=True),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("target_type", sa.String(64), nullable=False),
            sa.Column("target_id", sa.String(36), nullable=True),
            sa.Column("target_name", sa.String(255), nullable=True),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("old_value", sa.JSON(), nullable=True),
            sa.Column("new_value", sa.JSON(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    existing_indexes = (
        [idx["name"] for idx in inspector.get_indexes("audit_logs")]
        if "audit_logs" in inspector.get_table_names()
        else []
    )
    if "ix_audit_logs_actor_id" not in existing_indexes:
        op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    if "ix_audit_logs_action" not in existing_indexes:
        op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    if "ix_audit_logs_target_type" not in existing_indexes:
        op.create_index("ix_audit_logs_target_type", "audit_logs", ["target_type"])
    if "ix_audit_logs_target_id" not in existing_indexes:
        op.create_index("ix_audit_logs_target_id", "audit_logs", ["target_id"])


def downgrade() -> None:
    pass
