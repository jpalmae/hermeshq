"""Fix the built-in Pi policy defaults.

Revision ID: t4u5v6w7x8y9
Revises: s3t4u5v6w7x8
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "t4u5v6w7x8y9"
down_revision: str | Sequence[str] | None = "s3t4u5v6w7x8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE permission_policies
        SET tool_rules = '{"allow": ["read", "grep", "find", "ls"], "deny": ["bash", "shell", "terminal", "edit", "write", "file"]}'::json,
            updated_at = now()
        WHERE id = 'sys-pi-readonly'
        """
    )
    op.execute(
        """
        UPDATE permission_policies
        SET network_rules = '{"allow_domains": ["*.openai.com", "graph.microsoft.com"], "deny_all": true}'::json,
            updated_at = now()
        WHERE id = 'sys-pi-sandboxed'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE permission_policies
        SET tool_rules = '{"allow": ["read", "grep", "find", "ls"], "deny": ["*"]}'::json,
            updated_at = now()
        WHERE id = 'sys-pi-readonly'
        """
    )
    op.execute(
        """
        UPDATE permission_policies
        SET network_rules = '{"allow_domains": ["*.openai.com", "graph.microsoft.com"], "deny_all": false}'::json,
            updated_at = now()
        WHERE id = 'sys-pi-sandboxed'
        """
    )
