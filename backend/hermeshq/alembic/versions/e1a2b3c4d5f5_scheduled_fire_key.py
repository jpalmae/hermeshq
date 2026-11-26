from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f5"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_tasks_scheduled_fire_key",
        "tasks",
        [sa.text("(metadata->>'scheduled_fire_key')")],
        unique=True,
        postgresql_where=sa.text("metadata->>'scheduled_fire_key' IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_scheduled_fire_key", table_name="tasks")
