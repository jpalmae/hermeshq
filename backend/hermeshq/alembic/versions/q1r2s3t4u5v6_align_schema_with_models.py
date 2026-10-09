"""Align migrated schema with model metadata.

Revision ID: q1r2s3t4u5v6
Revises: p0q1r2s3t4u5
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "q1r2s3t4u5v6"
down_revision: str | Sequence[str] | None = "p0q1r2s3t4u5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "agent_assignments",
        "sharepoint_site_url",
        existing_type=sa.String(length=2048),
        type_=sa.Text(),
        existing_nullable=True,
    )

    op.execute(sa.text("UPDATE agents SET runtime_type = 'hermes' WHERE runtime_type IS NULL"))
    op.alter_column(
        "agents",
        "runtime_type",
        existing_type=sa.String(length=16),
        existing_server_default="hermes",
        nullable=False,
    )

    op.execute(sa.text("UPDATE app_settings SET m365_enabled_scopes = '[]'::json WHERE m365_enabled_scopes IS NULL"))
    op.alter_column(
        "app_settings",
        "m365_enabled_scopes",
        existing_type=sa.JSON(),
        existing_server_default="[]",
        nullable=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE public_chat_api_keys
            SET allowed_domains = COALESCE(allowed_domains, '{}'::varchar[]),
                requests_per_month = COALESCE(requests_per_month, 1000),
                tokens_per_month = COALESCE(tokens_per_month, 100000),
                is_active = COALESCE(is_active, true),
                created_at = COALESCE(created_at, now()),
                updated_at = COALESCE(updated_at, now())
            """
        )
    )
    op.alter_column(
        "public_chat_api_keys",
        "agent_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "allowed_domains",
        existing_type=postgresql.ARRAY(sa.String()),
        existing_server_default="{}",
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "requests_per_month",
        existing_type=sa.Integer(),
        existing_server_default="1000",
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "tokens_per_month",
        existing_type=sa.Integer(),
        existing_server_default="100000",
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "is_active",
        existing_type=sa.Boolean(),
        existing_server_default=sa.text("true"),
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "public_chat_api_keys",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE public_chat_sessions
            SET status = COALESCE(status, 'active'),
                last_activity = COALESCE(last_activity, now()),
                ttl_minutes = COALESCE(ttl_minutes, 10),
                created_at = COALESCE(created_at, now()),
                updated_at = COALESCE(updated_at, now())
            """
        )
    )
    op.alter_column(
        "public_chat_sessions",
        "api_key_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "agent_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "status",
        existing_type=sa.String(length=20),
        existing_server_default="active",
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "last_activity",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "ttl_minutes",
        existing_type=sa.Integer(),
        existing_server_default="10",
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "public_chat_sessions",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE public_chat_messages
            SET created_at = COALESCE(created_at, now()),
                updated_at = COALESCE(updated_at, now())
            """
        )
    )
    op.alter_column(
        "public_chat_messages",
        "session_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "public_chat_messages",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "public_chat_messages",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE public_chat_transcripts
            SET messages_json = COALESCE(messages_json, '[]'::json),
                archived_at = COALESCE(archived_at, now())
            """
        )
    )
    op.alter_column(
        "public_chat_transcripts",
        "messages_json",
        existing_type=sa.JSON(),
        existing_server_default="[]",
        nullable=False,
    )
    op.alter_column(
        "public_chat_transcripts",
        "archived_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "public_chat_transcripts",
        "archived_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
    op.alter_column(
        "public_chat_transcripts",
        "messages_json",
        existing_type=sa.JSON(),
        existing_server_default="[]",
        nullable=True,
    )
    op.alter_column(
        "public_chat_messages",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
    op.alter_column(
        "public_chat_messages",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
    op.alter_column(
        "public_chat_messages",
        "session_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    for column_name, column_type, server_default in (
        ("updated_at", sa.DateTime(timezone=True), sa.func.now()),
        ("created_at", sa.DateTime(timezone=True), sa.func.now()),
        ("ttl_minutes", sa.Integer(), "10"),
        ("last_activity", sa.DateTime(timezone=True), sa.func.now()),
        ("status", sa.String(length=20), "active"),
    ):
        op.alter_column(
            "public_chat_sessions",
            column_name,
            existing_type=column_type,
            existing_server_default=server_default,
            nullable=True,
        )
    op.alter_column(
        "public_chat_sessions",
        "agent_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        "public_chat_sessions",
        "api_key_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    for column_name, column_type, server_default in (
        ("updated_at", sa.DateTime(timezone=True), sa.func.now()),
        ("created_at", sa.DateTime(timezone=True), sa.func.now()),
        ("is_active", sa.Boolean(), sa.text("true")),
        ("tokens_per_month", sa.Integer(), "100000"),
        ("requests_per_month", sa.Integer(), "1000"),
        ("allowed_domains", postgresql.ARRAY(sa.String()), "{}"),
    ):
        op.alter_column(
            "public_chat_api_keys",
            column_name,
            existing_type=column_type,
            existing_server_default=server_default,
            nullable=True,
        )
    op.alter_column(
        "public_chat_api_keys",
        "agent_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        "app_settings",
        "m365_enabled_scopes",
        existing_type=sa.JSON(),
        existing_server_default="[]",
        nullable=True,
    )
    op.alter_column(
        "agents",
        "runtime_type",
        existing_type=sa.String(length=16),
        existing_server_default="hermes",
        nullable=True,
    )
    op.alter_column(
        "agent_assignments",
        "sharepoint_site_url",
        existing_type=sa.Text(),
        type_=sa.String(length=2048),
        existing_nullable=True,
    )
