"""Cloud multi-tenant F1: platforms, tenants, bindings + cloud role policies

Revision ID: bb22cc33dd44
Revises: aa11bb22cc33
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bb22cc33dd44"
down_revision: str | Sequence[str] | None = "aa11bb22cc33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_platforms",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="vergeos"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("api_url", sa.String(512), nullable=False),
        sa.Column("admin_credential_ref", sa.String(128), nullable=True),
        sa.Column("insecure_tls", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version_info", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "cloud_tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "platform_id",
            sa.String(36),
            sa.ForeignKey("cloud_platforms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_ref", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("credential_ref", sa.String(128), nullable=False),
        sa.Column("cache_ttl_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("platform_id", "tenant_ref", name="uq_cloud_tenant_platform_ref"),
    )
    op.create_index("ix_cloud_tenants_platform_id", "cloud_tenants", ["platform_id"])
    op.create_table(
        "cloud_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("cloud_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.String(36),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cloud_role", sa.String(32), nullable=False, server_default="viewer"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_cloud_binding_tenant_agent"),
    )
    op.create_index("ix_cloud_bindings_tenant_id", "cloud_bindings", ["tenant_id"])
    op.create_index("ix_cloud_bindings_agent_id", "cloud_bindings", ["agent_id"])

    op.execute(
        """
        INSERT INTO permission_policies
          (id, name, description, tool_rules, path_rules, command_rules, network_rules, approval_rules, is_system, created_at, updated_at)
        VALUES
          (
            'sys-role-cloud-viewer',
            'Role: Cloud Viewer',
            'Agente de cliente con acceso de solo lectura a su tenant cloud. Allowlist estricta: cualquier otra herramienta queda bloqueada, delegación deshabilitada y red cerrada.',
            '{"allow": ["vergeos_list_*", "vergeos_get_*", "vergeos_usage", "vergeos_events", "vergeos_report_*"], "deny": [], "deny_delegation": true}'::json,
            '{"allow_paths": [], "deny_paths": []}'::json,
            '{"allow": [], "deny": []}'::json,
            '{"allow_domains": [], "deny_all": true}'::json,
            '{"require_approval_for": []}'::json,
            true, now(), now()
          ),
          (
            'sys-role-cloud-operator',
            'Role: Cloud Operator',
            'Viewer + encender/apagar VMs y gestionar snapshots del tenant. Allowlist estricta, delegación deshabilitada, red cerrada.',
            '{"allow": ["vergeos_list_*", "vergeos_get_*", "vergeos_usage", "vergeos_events", "vergeos_report_*", "vergeos_vm_power", "vergeos_snapshot_*"], "deny": [], "deny_delegation": true}'::json,
            '{"allow_paths": [], "deny_paths": []}'::json,
            '{"allow": [], "deny": []}'::json,
            '{"allow_domains": [], "deny_all": true}'::json,
            '{"require_approval_for": []}'::json,
            true, now(), now()
          ),
          (
            'sys-role-cloud-admin',
            'Role: Cloud Admin',
            'Operator + crear y redimensionar VMs del tenant. Sin delete (por diseño). Allowlist estricta, delegación deshabilitada, red cerrada.',
            '{"allow": ["vergeos_*"], "deny": [], "deny_delegation": true}'::json,
            '{"allow_paths": [], "deny_paths": []}'::json,
            '{"allow": [], "deny": []}'::json,
            '{"allow_domains": [], "deny_all": true}'::json,
            '{"require_approval_for": []}'::json,
            true, now(), now()
          )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM permission_policies WHERE id IN "
        "('sys-role-cloud-viewer', 'sys-role-cloud-operator', 'sys-role-cloud-admin')"
    )
    op.drop_index("ix_cloud_bindings_agent_id", table_name="cloud_bindings")
    op.drop_index("ix_cloud_bindings_tenant_id", table_name="cloud_bindings")
    op.drop_table("cloud_bindings")
    op.drop_index("ix_cloud_tenants_platform_id", table_name="cloud_tenants")
    op.drop_table("cloud_tenants")
    op.drop_table("cloud_platforms")
