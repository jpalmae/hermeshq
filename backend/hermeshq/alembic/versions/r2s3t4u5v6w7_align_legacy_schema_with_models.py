"""Align legacy schema with model metadata.

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r2s3t4u5v6w7"
down_revision: str | Sequence[str] | None = "q1r2s3t4u5v6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agents
            SET runtime_profile = COALESCE(runtime_profile, 'standard'),
                integration_configs = COALESCE(integration_configs, '{}'::json),
                is_system_agent = COALESCE(is_system_agent, false),
                is_archived = COALESCE(is_archived, false)
            """
        )
    )
    for column_name, column_type in (
        ("runtime_profile", sa.String(length=32)),
        ("integration_configs", sa.JSON()),
        ("is_system_agent", sa.Boolean()),
        ("is_archived", sa.Boolean()),
    ):
        op.alter_column("agents", column_name, existing_type=column_type, nullable=False)

    op.execute(
        sa.text(
            """
            UPDATE app_settings
            SET enabled_integration_packages = COALESCE(enabled_integration_packages, '[]'::json)
            """
        )
    )
    op.alter_column(
        "app_settings",
        "enabled_integration_packages",
        existing_type=sa.JSON(),
        nullable=False,
    )

    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET board_column = COALESCE(
                    board_column,
                    CASE
                        WHEN status = 'running' THEN 'running'
                        WHEN status = 'completed' THEN 'done'
                        WHEN status IN ('failed', 'cancelled') THEN 'failed'
                        ELSE 'inbox'
                    END
                ),
                board_order = COALESCE(
                    board_order,
                    FLOOR(EXTRACT(EPOCH FROM COALESCE(queued_at, now())) * 1000)::bigint
                ),
                board_manual = COALESCE(board_manual, false)
            """
        )
    )
    for column_name, column_type in (
        ("board_column", sa.String(length=32)),
        ("board_order", sa.BigInteger()),
        ("board_manual", sa.Boolean()),
    ):
        op.alter_column("tasks", column_name, existing_type=column_type, nullable=False)

    op.execute(
        sa.text(
            """
            UPDATE users
            SET auth_source = COALESCE(auth_source, 'local'),
                role = COALESCE(role, 'user'),
                is_active = COALESCE(is_active, true),
                theme_preference = COALESCE(theme_preference, 'default'),
                locale_preference = COALESCE(locale_preference, 'default')
            """
        )
    )
    for column_name, column_type in (
        ("auth_source", sa.String(length=32)),
        ("role", sa.String(length=16)),
        ("is_active", sa.Boolean()),
        ("theme_preference", sa.String(length=16)),
        ("locale_preference", sa.String(length=16)),
    ):
        op.alter_column("users", column_name, existing_type=column_type, nullable=False)

    for index_name, table_name, columns in (
        ("ix_agents_friendly_name", "agents", ["friendly_name"]),
        ("ix_agents_hermes_version", "agents", ["hermes_version"]),
        ("ix_agents_is_archived", "agents", ["is_archived"]),
        ("ix_agents_is_system_agent", "agents", ["is_system_agent"]),
        ("ix_agents_runtime_profile", "agents", ["runtime_profile"]),
        ("ix_tasks_board_column", "tasks", ["board_column"]),
        ("ix_tasks_board_order", "tasks", ["board_order"]),
        ("ix_users_role", "users", ["role"]),
    ):
        op.create_index(index_name, table_name, columns, unique=False, if_not_exists=True)


def downgrade() -> None:
    pass
