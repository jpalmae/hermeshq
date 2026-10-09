"""add permission_policies and pi agent fields

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o9p0q1r2s3t4"
down_revision: str | Sequence[str] | None = "n8o9p0q1r2s3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "permission_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("tool_rules", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("path_rules", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("command_rules", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("network_rules", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("approval_rules", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.add_column("agents", sa.Column("runtime_type", sa.String(16), server_default="hermes"))
    op.add_column("agents", sa.Column("pi_config", sa.JSON, nullable=True))
    op.add_column(
        "agents",
        sa.Column(
            "permission_policy_id",
            sa.String(36),
            sa.ForeignKey("permission_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_agents_permission_policy_id", "agents", ["permission_policy_id"])

    op.execute(
        """
        INSERT INTO permission_policies (id, name, description, tool_rules, path_rules, command_rules, network_rules, approval_rules, is_system, created_at, updated_at)
        VALUES
        (
          'sys-pi-developer',
          'Pi Developer',
          'Standard Pi agent — read/write/bash with safety guards',
          '{"allow": ["read", "bash", "edit", "write", "grep", "find", "ls"], "deny": []}'::json,
          '{"allow_paths": ["/workspace/**"], "deny_paths": ["**/.env", "**/node_modules/**"]}'::json,
          '{"allow": [], "deny": ["rm -rf /", "sudo *"]}'::json,
          '{"allow_domains": [], "deny_all": false}'::json,
          '{"require_approval_for": ["bash:sudo *", "bash:rm *"], "auto_approve_threshold": "medium"}'::json,
          true,
          now(),
          now()
        ),
        (
          'sys-pi-readonly',
          'Pi Read-Only',
          'Read-only Pi agent — no writes, no bash execution',
          '{"allow": ["read", "grep", "find", "ls"], "deny": ["*"]}'::json,
          '{"allow_paths": ["/workspace/**"], "deny_paths": ["/etc/**", "/root/**"]}'::json,
          '{"allow": [], "deny": ["*"]}'::json,
          '{"allow_domains": [], "deny_all": true}'::json,
          '{"require_approval_for": ["*"], "auto_approve_threshold": "none"}'::json,
          true,
          now(),
          now()
        ),
        (
          'sys-pi-full',
          'Pi Full Access',
          'Unrestricted Pi agent — all tools, all paths',
          '{"allow": ["*"], "deny": []}'::json,
          '{"allow_paths": ["**"], "deny_paths": []}'::json,
          '{"allow": ["*"], "deny": []}'::json,
          '{"allow_domains": [], "deny_all": false}'::json,
          '{"require_approval_for": [], "auto_approve_threshold": "high"}'::json,
          true,
          now(),
          now()
        ),
        (
          'sys-pi-sandboxed',
          'Pi Sandboxed',
          'Sandboxed Pi agent — limited tools, protected paths, network restricted',
          '{"allow": ["read", "bash", "edit"], "deny": ["write:**/.env"]}'::json,
          '{"allow_paths": ["/workspace/**"], "deny_paths": ["/etc/**", "/root/**", "**/.ssh/**"]}'::json,
          '{"allow": [], "deny": ["sudo *", "chmod 777 *", "curl * | sh"]}'::json,
          '{"allow_domains": ["*.openai.com", "graph.microsoft.com"], "deny_all": false}'::json,
          '{"require_approval_for": ["bash:sudo *"], "auto_approve_threshold": "low"}'::json,
          true,
          now(),
          now()
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_agents_permission_policy_id", table_name="agents")
    op.drop_column("agents", "permission_policy_id")
    op.drop_column("agents", "pi_config")
    op.drop_column("agents", "runtime_type")
    op.drop_table("permission_policies")
