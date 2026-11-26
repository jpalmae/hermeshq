from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f7"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("approval_central_mode", sa.String(16), nullable=False, server_default="heredar"),
    )
    op.create_check_constraint(
        "ck_agents_approval_central_mode", "agents", "approval_central_mode IN ('heredar', 'activada', 'desactivada')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_agents_approval_central_mode", "agents", type_="check")
    op.drop_column("agents", "approval_central_mode")
