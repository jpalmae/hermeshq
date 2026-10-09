"""repair missing mfa schema

Repair migration: applies changes from a1b2c3d4e5f6 that were skipped due to
a version-table/schema desync. Uses IF NOT EXISTS so it is safe on databases
that already have these objects.

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-06-11 00:00:00.000000

"""

from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mfa_codes (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL,
            code_hash VARCHAR(128) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            ip_address VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_mfa_codes_user_id ON mfa_codes (user_id)")
    op.execute("""
        ALTER TABLE app_settings
        ADD COLUMN IF NOT EXISTS mfa_email_enabled BOOLEAN NOT NULL DEFAULT false
    """)


def downgrade() -> None:
    pass
