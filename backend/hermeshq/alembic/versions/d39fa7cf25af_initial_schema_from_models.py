"""Initial schema.

Revision ID: d39fa7cf25af
Revises:
Create Date: 2026-05-22 17:37:04.947999
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d39fa7cf25af"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_templates_name"), "agent_templates", ["name"], unique=True)

    op.create_table(
        "app_settings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("app_name", sa.String(length=128), nullable=True),
        sa.Column("app_short_name", sa.String(length=48), nullable=True),
        sa.Column("theme_mode", sa.String(length=16), nullable=True),
        sa.Column("default_locale", sa.String(length=8), nullable=True),
        sa.Column("default_provider", sa.String(length=64), nullable=True),
        sa.Column("default_model", sa.String(length=255), nullable=True),
        sa.Column("default_api_key_ref", sa.String(length=128), nullable=True),
        sa.Column("default_base_url", sa.String(length=512), nullable=True),
        sa.Column("default_hermes_version", sa.String(length=32), nullable=True),
        sa.Column("default_tui_skin", sa.String(length=128), nullable=True),
        sa.Column("enabled_integration_packages", sa.JSON(), nullable=False),
        sa.Column("tui_skin_filename", sa.String(length=255), nullable=True),
        sa.Column("logo_filename", sa.String(length=255), nullable=True),
        sa.Column("favicon_filename", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "hermes_versions",
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("release_tag", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("version"),
    )

    op.create_table(
        "nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ssh_user", sa.String(length=64), nullable=True),
        sa.Column("ssh_port", sa.Integer(), nullable=False),
        sa.Column("hermes_path", sa.String(length=255), nullable=False),
        sa.Column("max_agents", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("system_info", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nodes_name"), "nodes", ["name"], unique=True)
    op.create_index(op.f("ix_nodes_status"), "nodes", ["status"], unique=False)

    op.create_table(
        "oidc_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("client_secret", sa.String(length=512), nullable=False),
        sa.Column("discovery_url", sa.String(length=1024), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_provision", sa.Boolean(), nullable=False),
        sa.Column("allowed_domains", sa.Text(), nullable=True),
        sa.Column("icon_slug", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_oidc_providers_slug"), "oidc_providers", ["slug"], unique=True)

    op.create_table(
        "providers",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("runtime_provider", sa.String(length=64), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("default_model", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("docs_url", sa.String(length=512), nullable=True),
        sa.Column("secret_placeholder", sa.String(length=128), nullable=True),
        sa.Column("supports_secret_ref", sa.Boolean(), nullable=False),
        sa.Column("supports_custom_base_url", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("slug"),
    )
    op.create_index(
        op.f("ix_providers_runtime_provider"),
        "providers",
        ["runtime_provider"],
        unique=False,
    )

    op.create_table(
        "secrets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("value_enc", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_secrets_name"), "secrets", ["name"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("auth_source", sa.String(length=32), nullable=False),
        sa.Column("oidc_subject", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("theme_preference", sa.String(length=16), nullable=False),
        sa.Column("locale_preference", sa.String(length=16), nullable=False),
        sa.Column("avatar_filename", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_auth_source"), "users", ["auth_source"], unique=False)
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_oidc_subject"), "users", ["oidc_subject"], unique=False)
    op.create_index(op.f("ix_users_role"), "users", ["role"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)

    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("friendly_name", sa.String(length=128), nullable=True),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("avatar_filename", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("run_mode", sa.String(length=20), nullable=False),
        sa.Column("runtime_profile", sa.String(length=32), nullable=False),
        sa.Column("hermes_version", sa.String(length=32), nullable=True),
        sa.Column("approval_mode", sa.String(length=32), nullable=True),
        sa.Column("tool_progress_mode", sa.String(length=16), nullable=True),
        sa.Column("gateway_notifications_mode", sa.String(length=16), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("api_key_ref", sa.String(length=128), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("fallback_provider", sa.String(length=64), nullable=True),
        sa.Column("fallback_model", sa.String(length=255), nullable=True),
        sa.Column("fallback_api_key_ref", sa.String(length=128), nullable=True),
        sa.Column("fallback_base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled_toolsets", sa.JSON(), nullable=False),
        sa.Column("disabled_toolsets", sa.JSON(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("integration_configs", sa.JSON(), nullable=False),
        sa.Column("mcp_servers", sa.JSON(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("soul_md", sa.Text(), nullable=True),
        sa.Column("personality", sa.String(length=64), nullable=True),
        sa.Column("context_files", sa.JSON(), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("working_directory", sa.Text(), nullable=True),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("max_tokens_per_task", sa.Integer(), nullable=False),
        sa.Column("auto_approve_cmds", sa.Boolean(), nullable=False),
        sa.Column("command_allowlist", sa.JSON(), nullable=False),
        sa.Column("is_system_agent", sa.Boolean(), nullable=False),
        sa.Column("system_scope", sa.String(length=32), nullable=True),
        sa.Column("can_receive_tasks", sa.Boolean(), nullable=False),
        sa.Column("can_send_tasks", sa.Boolean(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("supervisor_agent_id", sa.String(length=36), nullable=True),
        sa.Column("team_tags", sa.JSON(), nullable=False),
        sa.Column("total_tasks", sa.Integer(), nullable=False),
        sa.Column("total_tokens_used", sa.Integer(), nullable=False),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supervisor_agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agents_friendly_name"), "agents", ["friendly_name"], unique=False)
    op.create_index(op.f("ix_agents_hermes_version"), "agents", ["hermes_version"], unique=False)
    op.create_index(op.f("ix_agents_is_archived"), "agents", ["is_archived"], unique=False)
    op.create_index(op.f("ix_agents_is_system_agent"), "agents", ["is_system_agent"], unique=False)
    op.create_index(op.f("ix_agents_name"), "agents", ["name"], unique=False)
    op.create_index(op.f("ix_agents_node_id"), "agents", ["node_id"], unique=False)
    op.create_index(op.f("ix_agents_runtime_profile"), "agents", ["runtime_profile"], unique=False)
    op.create_index(op.f("ix_agents_slug"), "agents", ["slug"], unique=True)
    op.create_index(op.f("ix_agents_status"), "agents", ["status"], unique=False)

    op.create_table(
        "mcp_access_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("client_name", sa.String(length=128), nullable=True),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("allowed_agent_ids", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mcp_access_tokens_created_by_user_id"),
        "mcp_access_tokens",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mcp_access_tokens_expires_at"),
        "mcp_access_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mcp_access_tokens_is_active"),
        "mcp_access_tokens",
        ["is_active"],
        unique=False,
    )
    op.create_index(op.f("ix_mcp_access_tokens_name"), "mcp_access_tokens", ["name"], unique=False)
    op.create_index(
        op.f("ix_mcp_access_tokens_token_hash"),
        "mcp_access_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_mcp_access_tokens_token_prefix"),
        "mcp_access_tokens",
        ["token_prefix"],
        unique=False,
    )

    op.create_table(
        "agent_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("assigned_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "agent_id", name="uq_agent_assignments_user_agent"),
    )
    op.create_index(
        op.f("ix_agent_assignments_agent_id"),
        "agent_assignments",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_assignments_user_id"),
        "agent_assignments",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "integration_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("template", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_agent_id", sa.String(length=36), nullable=True),
        sa.Column("last_validation", sa.JSON(), nullable=True),
        sa.Column("published_package_slug", sa.String(length=128), nullable=True),
        sa.Column("published_package_version", sa.String(length=32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_integration_drafts_slug"), "integration_drafts", ["slug"], unique=True)
    op.create_index(op.f("ix_integration_drafts_status"), "integration_drafts", ["status"], unique=False)
    op.create_index(
        op.f("ix_integration_drafts_template"),
        "integration_drafts",
        ["template"],
        unique=False,
    )

    op.create_table(
        "messaging_channels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("secret_ref", sa.String(length=128), nullable=True),
        sa.Column("allowed_user_ids", sa.JSON(), nullable=False),
        sa.Column("home_chat_id", sa.String(length=128), nullable=True),
        sa.Column("home_chat_name", sa.String(length=128), nullable=True),
        sa.Column("require_mention", sa.Boolean(), nullable=False),
        sa.Column("free_response_chat_ids", sa.JSON(), nullable=False),
        sa.Column("unauthorized_dm_behavior", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "platform", name="uq_messaging_channels_agent_platform"),
    )
    op.create_index(
        op.f("ix_messaging_channels_agent_id"),
        "messaging_channels",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messaging_channels_platform"),
        "messaging_channels",
        ["platform"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messaging_channels_status"),
        "messaging_channels",
        ["status"],
        unique=False,
    )

    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scheduled_tasks_agent_id"),
        "scheduled_tasks",
        ["agent_id"],
        unique=False,
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("parent_task_id", sa.String(length=36), nullable=True),
        sa.Column("source_agent_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("system_override", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("board_column", sa.String(length=32), nullable=False),
        sa.Column("board_order", sa.BigInteger(), nullable=False),
        sa.Column("board_manual", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("messages_json", sa.JSON(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["source_agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_agent_id"), "tasks", ["agent_id"], unique=False)
    op.create_index(op.f("ix_tasks_board_column"), "tasks", ["board_column"], unique=False)
    op.create_index(op.f("ix_tasks_board_order"), "tasks", ["board_order"], unique=False)
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"], unique=False)

    op.create_table(
        "terminal_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("command_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("input_transcript", sa.Text(), nullable=False),
        sa.Column("output_transcript", sa.Text(), nullable=False),
        sa.Column("transcript_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_terminal_sessions_agent_id"),
        "terminal_sessions",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_terminal_sessions_node_id"),
        "terminal_sessions",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_terminal_sessions_status"),
        "terminal_sessions",
        ["status"],
        unique=False,
    )

    op.create_table(
        "activity_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("node_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activity_logs_agent_id"), "activity_logs", ["agent_id"], unique=False)
    op.create_index(
        op.f("ix_activity_logs_event_type"),
        "activity_logs",
        ["event_type"],
        unique=False,
    )
    op.create_index(op.f("ix_activity_logs_node_id"), "activity_logs", ["node_id"], unique=False)
    op.create_index(op.f("ix_activity_logs_task_id"), "activity_logs", ["task_id"], unique=False)

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("from_agent_id", sa.String(length=36), nullable=False),
        sa.Column("to_agent_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["to_agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_messages_from_agent_id"),
        "agent_messages",
        ["from_agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_messages_task_id"),
        "agent_messages",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_messages_to_agent_id"),
        "agent_messages",
        ["to_agent_id"],
        unique=False,
    )

    op.create_table(
        "conversation_threads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("last_task_id", sa.String(length=36), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "user_id", name="uq_conversation_threads_agent_user"),
    )
    op.create_index(
        op.f("ix_conversation_threads_agent_id"),
        "conversation_threads",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_threads_user_id"),
        "conversation_threads",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("conversation_threads")
    op.drop_table("agent_messages")
    op.drop_table("activity_logs")
    op.drop_table("terminal_sessions")
    op.drop_table("tasks")
    op.drop_table("scheduled_tasks")
    op.drop_table("messaging_channels")
    op.drop_table("integration_drafts")
    op.drop_table("agent_assignments")
    op.drop_table("mcp_access_tokens")
    op.drop_table("agents")
    op.drop_table("users")
    op.drop_table("secrets")
    op.drop_table("providers")
    op.drop_table("oidc_providers")
    op.drop_table("nodes")
    op.drop_table("hermes_versions")
    op.drop_table("app_settings")
    op.drop_table("agent_templates")
