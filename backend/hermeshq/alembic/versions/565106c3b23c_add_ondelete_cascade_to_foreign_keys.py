"""add_ondelete_cascade_to_foreign_keys

Revision ID: 565106c3b23c
Revises: a3b4c5d6e7f8
Create Date: 2026-05-30 23:47:26.665280

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '565106c3b23c'
down_revision: str | None = 'a3b4c5d6e7f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk_exists(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(
        fk["name"] == constraint_name
        for fk in insp.get_foreign_keys(table_name)
    )


def _replace_fk(
    table: str,
    old_name: str,
    new_name: str,
    ref_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    ondelete: str,
) -> None:
    if _fk_exists(table, new_name):
        return
    if _fk_exists(table, old_name):
        op.drop_constraint(old_name, table, type_="foreignkey")
    op.create_foreign_key(new_name, table, ref_table, local_cols, remote_cols, ondelete=ondelete)


def upgrade() -> None:
    # --- activity_logs: SET NULL on agent/task/node deletion ---
    _replace_fk("activity_logs", op.f("activity_logs_agent_id_fkey"), "activity_logs_agent_id_cascade_fkey",
                "agents", ["agent_id"], ["id"], "SET NULL")
    _replace_fk("activity_logs", op.f("activity_logs_node_id_fkey"), "activity_logs_node_id_cascade_fkey",
                "nodes", ["node_id"], ["id"], "SET NULL")
    _replace_fk("activity_logs", op.f("activity_logs_task_id_fkey"), "activity_logs_task_id_cascade_fkey",
                "tasks", ["task_id"], ["id"], "SET NULL")

    # --- agent_messages: CASCADE on agent deletion, SET NULL on task deletion ---
    _replace_fk("agent_messages", op.f("agent_messages_task_id_fkey"), "agent_messages_task_id_cascade_fkey",
                "tasks", ["task_id"], ["id"], "SET NULL")
    _replace_fk("agent_messages", op.f("agent_messages_from_agent_id_fkey"), "agent_messages_from_agent_id_cascade_fkey",
                "agents", ["from_agent_id"], ["id"], "CASCADE")
    _replace_fk("agent_messages", op.f("agent_messages_to_agent_id_fkey"), "agent_messages_to_agent_id_cascade_fkey",
                "agents", ["to_agent_id"], ["id"], "CASCADE")

    # --- agents: SET NULL on supervisor agent deletion ---
    _replace_fk("agents", op.f("agents_supervisor_agent_id_fkey"), "agents_supervisor_agent_id_cascade_fkey",
                "agents", ["supervisor_agent_id"], ["id"], "SET NULL")

    # --- tasks: SET NULL on parent task / source agent deletion ---
    _replace_fk("tasks", op.f("tasks_parent_task_id_fkey"), "tasks_parent_task_id_cascade_fkey",
                "tasks", ["parent_task_id"], ["id"], "SET NULL")
    _replace_fk("tasks", op.f("tasks_source_agent_id_fkey"), "tasks_source_agent_id_cascade_fkey",
                "agents", ["source_agent_id"], ["id"], "SET NULL")

    # --- terminal_sessions: CASCADE on agent deletion, SET NULL on node deletion ---
    _replace_fk("terminal_sessions", op.f("terminal_sessions_agent_id_fkey"), "terminal_sessions_agent_id_cascade_fkey",
                "agents", ["agent_id"], ["id"], "CASCADE")
    _replace_fk("terminal_sessions", op.f("terminal_sessions_node_id_fkey"), "terminal_sessions_node_id_cascade_fkey",
                "nodes", ["node_id"], ["id"], "SET NULL")


def downgrade() -> None:
    # --- terminal_sessions: revert to RESTRICT (no ondelete) ---
    _replace_fk("terminal_sessions", "terminal_sessions_node_id_cascade_fkey", op.f("terminal_sessions_node_id_fkey"),
                "nodes", ["node_id"], ["id"], "RESTRICT")
    _replace_fk("terminal_sessions", "terminal_sessions_agent_id_cascade_fkey", op.f("terminal_sessions_agent_id_fkey"),
                "agents", ["agent_id"], ["id"], "RESTRICT")

    # --- tasks: revert ---
    _replace_fk("tasks", "tasks_source_agent_id_cascade_fkey", op.f("tasks_source_agent_id_fkey"),
                "agents", ["source_agent_id"], ["id"], "RESTRICT")
    _replace_fk("tasks", "tasks_parent_task_id_cascade_fkey", op.f("tasks_parent_task_id_fkey"),
                "tasks", ["parent_task_id"], ["id"], "RESTRICT")

    # --- agents: revert ---
    _replace_fk("agents", "agents_supervisor_agent_id_cascade_fkey", op.f("agents_supervisor_agent_id_fkey"),
                "agents", ["supervisor_agent_id"], ["id"], "RESTRICT")

    # --- agent_messages: revert ---
    _replace_fk("agent_messages", "agent_messages_to_agent_id_cascade_fkey", op.f("agent_messages_to_agent_id_fkey"),
                "agents", ["to_agent_id"], ["id"], "RESTRICT")
    _replace_fk("agent_messages", "agent_messages_from_agent_id_cascade_fkey", op.f("agent_messages_from_agent_id_fkey"),
                "agents", ["from_agent_id"], ["id"], "RESTRICT")
    _replace_fk("agent_messages", "agent_messages_task_id_cascade_fkey", op.f("agent_messages_task_id_fkey"),
                "tasks", ["task_id"], ["id"], "RESTRICT")

    # --- activity_logs: revert ---
    _replace_fk("activity_logs", "activity_logs_task_id_cascade_fkey", op.f("activity_logs_task_id_fkey"),
                "tasks", ["task_id"], ["id"], "RESTRICT")
    _replace_fk("activity_logs", "activity_logs_node_id_cascade_fkey", op.f("activity_logs_node_id_fkey"),
                "nodes", ["node_id"], ["id"], "RESTRICT")
    _replace_fk("activity_logs", "activity_logs_agent_id_cascade_fkey", op.f("activity_logs_agent_id_fkey"),
                "agents", ["agent_id"], ["id"], "RESTRICT")
