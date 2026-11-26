from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f3"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _marcas_de_tiempo() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "bot_channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("platform", sa.String(32), nullable=False, server_default="microsoft_teams"),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("azure_app_id", sa.String(64), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("service_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("require_channel_header", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_marcas_de_tiempo(),
    )
    op.create_index(
        "uq_bot_channels_default",
        "bot_channels",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        if_not_exists=True,
    )
    op.create_table(
        "user_channel_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "bot_channel_id", sa.String(36), sa.ForeignKey("bot_channels.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("external_user_id", sa.String(255), nullable=True),
        sa.Column("aad_object_id", sa.String(64), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_marcas_de_tiempo(),
        sa.UniqueConstraint("bot_channel_id", "external_user_id", name="uq_user_channel_identities_externo"),
        sa.UniqueConstraint("bot_channel_id", "aad_object_id", "tenant_id", name="uq_user_channel_identities_aad"),
    )
    op.create_index("ix_user_channel_identities_user_id", "user_channel_identities", ["user_id"], if_not_exists=True)
    op.create_index(
        "ix_user_channel_identities_bot_channel_id", "user_channel_identities", ["bot_channel_id"], if_not_exists=True
    )
    op.create_table(
        "public_chat_delivery_prefs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "cron_key_id", sa.String(36), sa.ForeignKey("public_chat_api_keys.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "notice_key_id",
            sa.String(36),
            sa.ForeignKey("public_chat_api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_marcas_de_tiempo(),
        sa.UniqueConstraint("user_id", "agent_id", name="uq_public_chat_delivery_prefs_par"),
    )
    op.add_column(
        "public_chat_api_keys",
        sa.Column(
            "bot_channel_id", sa.String(36), sa.ForeignKey("bot_channels.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index(
        "ix_public_chat_api_keys_bot_channel_id", "public_chat_api_keys", ["bot_channel_id"], if_not_exists=True
    )
    op.add_column("public_chat_api_keys", sa.Column("last_poll_instance", sa.String(64), nullable=True))
    op.add_column(
        "public_chat_sessions",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("public_chat_sessions", sa.Column("aad_object_id", sa.String(64), nullable=True))
    op.add_column(
        "scheduled_tasks",
        sa.Column(
            "delivery_key_id",
            sa.String(36),
            sa.ForeignKey("public_chat_api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "public_chat_notifications",
        sa.Column(
            "bot_channel_id", sa.String(36), sa.ForeignKey("bot_channels.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.add_column("public_chat_notifications", sa.Column("recipient_external_id", sa.String(255), nullable=True))
    op.add_column("public_chat_notifications", sa.Column("recipient_aad_object_id", sa.String(64), nullable=True))
    op.add_column("public_chat_notifications", sa.Column("origin", sa.String(32), nullable=True))

    bind = op.get_bind()
    servicio = bind.execute(sa.text("SELECT id FROM users WHERE username = 'bridge-teams' LIMIT 1")).scalar()
    bind.execute(
        sa.text(
            "INSERT INTO bot_channels (id, slug, platform, display_name, service_user_id, is_default, is_active, "
            "require_channel_header, created_at, updated_at) "
            "SELECT :id, 'teams-principal', 'microsoft_teams', 'Teams principal', :servicio, true, true, false, "
            "now(), now() "
            "WHERE NOT EXISTS (SELECT 1 FROM bot_channels WHERE slug = 'teams-principal' OR is_default)"
        ),
        {"id": str(uuid4()), "servicio": servicio},
    )


def downgrade() -> None:
    op.drop_column("public_chat_notifications", "origin")
    op.drop_column("public_chat_notifications", "recipient_aad_object_id")
    op.drop_column("public_chat_notifications", "recipient_external_id")
    op.drop_column("public_chat_notifications", "bot_channel_id")
    op.drop_column("scheduled_tasks", "delivery_key_id")
    op.drop_column("public_chat_sessions", "aad_object_id")
    op.drop_column("public_chat_sessions", "user_id")
    op.drop_column("public_chat_api_keys", "last_poll_instance")
    op.drop_index("ix_public_chat_api_keys_bot_channel_id", table_name="public_chat_api_keys")
    op.drop_column("public_chat_api_keys", "bot_channel_id")
    op.drop_table("public_chat_delivery_prefs")
    op.drop_index("ix_user_channel_identities_bot_channel_id", table_name="user_channel_identities")
    op.drop_index("ix_user_channel_identities_user_id", table_name="user_channel_identities")
    op.drop_table("user_channel_identities")
    op.drop_index("uq_bot_channels_default", table_name="bot_channels")
    op.drop_table("bot_channels")
