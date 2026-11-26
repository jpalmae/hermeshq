from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f8"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("approval_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "approval_policy")
