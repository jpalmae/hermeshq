from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f4"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("output_format", sa.Text(), nullable=True))
    op.add_column(
        "scheduled_tasks",
        sa.Column("definition_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "definition_version")
    op.drop_column("scheduled_tasks", "output_format")
