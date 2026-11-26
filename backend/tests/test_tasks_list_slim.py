from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import requires_database
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermeshq.models.task import Task
from hermeshq.routers.tasks import get_task, list_tasks
from hermeshq.schemas.task import TaskRead

_BIG_MESSAGES = [{"role": "assistant", "content": "x" * 3000} for _ in range(20)]
_BIG_TOOL_CALLS = [{"name": "tool", "status": "ok", "payload": {"blob": "y" * 2000}} for _ in range(5)]


def _make_task(task_id: str = "t1", status: str = "completed", **overrides) -> Task:
    fields = dict(
        id=task_id,
        agent_id="a1",
        created_by_user_id="u1",
        title="Task",
        prompt="prompt",
        status=status,
        board_column="inbox",
        board_order=1,
        board_manual=False,
        priority=5,
        response=None if status in {"queued", "running"} else "response text",
        error_message=None,
        messages_json=list(_BIG_MESSAGES),
        tool_calls=list(_BIG_TOOL_CALLS),
        tokens_used=0,
        iterations=0,
        queued_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        started_at=None,
        completed_at=None,
        metadata_json={"conversation": True},
    )
    fields.update(overrides)
    return Task(**fields)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id="u1", role="admin")


def _db_returning(rows: list[Task]) -> AsyncMock:
    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=exec_result)
    return db


class TestSlimTaskListUnit:
    async def test_list_omits_heavy_columns(self):
        running = _make_task("t0", status="running")
        done = _make_task("t1", status="completed")
        db = _db_returning([running, done])

        items = await list_tasks(agent_id="a1", limit=200, offset=0, current_user=_admin(), db=db)

        assert [item.id for item in items] == ["t0", "t1"]
        for item in items:
            assert not hasattr(item, "messages_json")
            assert not hasattr(item, "tool_calls")
        statuses = {item.id: item.status for item in items}
        assert statuses["t0"] == "running"
        assert items[1].response == "response text"

    async def test_detail_keeps_heavy_columns(self):
        task = _make_task("t1", status="completed")
        db = AsyncMock()
        db.get = AsyncMock(return_value=task)

        with patch("hermeshq.routers.tasks.ensure_agent_access", AsyncMock(return_value=task)):
            detail = await get_task(task_id="t1", current_user=_admin(), db=db)

        assert isinstance(detail, TaskRead)
        assert detail.messages_json == _BIG_MESSAGES
        assert detail.tool_calls == _BIG_TOOL_CALLS

    async def test_list_payload_much_smaller_than_detail(self):
        from hermeshq.schemas.task import TaskListRead

        tasks = [_make_task(f"t{i}") for i in range(20)]
        full = [TaskRead.model_validate(t).model_dump(mode="json", by_alias=True) for t in tasks]
        slim = [TaskListRead.model_validate(t).model_dump(mode="json", by_alias=True) for t in tasks]
        full_bytes = len(json.dumps(full).encode())
        slim_bytes = len(json.dumps(slim).encode())

        assert slim_bytes < full_bytes * 0.1
        print(
            f"\n[QF-8] list payload: full={full_bytes} B slim={slim_bytes} B "
            f"reduction={100 * (1 - slim_bytes / full_bytes):.1f}% "
            f"({len(tasks)} tasks, ~{full_bytes // len(tasks)} B/task full, "
            f"~{slim_bytes // len(tasks)} B/task slim)"
        )


@requires_database
@pytest.mark.integration
class TestSlimTaskListIntegration:
    @pytest.fixture
    async def seeded(self, db_session):
        from hermeshq.models.agent import Agent
        from hermeshq.models.node import Node
        from hermeshq.models.user import User

        admin = User(
            username="slim-admin",
            display_name="Slim Admin",
            password_hash="x",
            role="admin",
            is_active=True,
        )
        db_session.add(admin)
        node = Node(name="slim-node", hostname="localhost")
        db_session.add(node)
        await db_session.flush()
        agent = Agent(node_id=node.id, name="slim-agent", slug="slim-agent", workspace_path="/tmp/slim")
        db_session.add(agent)
        await db_session.flush()

        tasks = []
        for idx in range(6):
            status = "running" if idx == 0 else "completed"
            task = Task(
                agent_id=agent.id,
                created_by_user_id=admin.id,
                title=f"Task {idx}",
                prompt=f"prompt {idx}",
                status=status,
                response=None if status == "running" else f"response {idx}",
                messages_json=list(_BIG_MESSAGES),
                tool_calls=list(_BIG_TOOL_CALLS),
                metadata_json={"conversation": True},
            )
            db_session.add(task)
            tasks.append(task)
        await db_session.commit()
        for task in tasks:
            await db_session.refresh(task)
        db_session.expunge_all()
        return {"admin_id": admin.id, "agent_id": agent.id, "running_id": tasks[0].id, "detail_id": tasks[1].id}

    @pytest.fixture
    async def client(self, db_session, seeded):
        from hermeshq.core.security import get_current_user
        from hermeshq.database import get_db_session
        from hermeshq.models.user import User
        from hermeshq.routers.runtime_ledger import router as runtime_ledger_router
        from hermeshq.routers.tasks import router as tasks_router

        admin = await db_session.get(User, seeded["admin_id"])
        app = FastAPI()
        app.include_router(tasks_router, prefix="/api")
        app.include_router(runtime_ledger_router, prefix="/api")

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_db_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: admin

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client

    async def test_list_response_excludes_heavy_columns(self, client, seeded):
        response = await client.get("/api/tasks", params={"agent_id": seeded["agent_id"]})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 6
        for item in body:
            assert "messages_json" not in item
            assert "tool_calls" not in item
            assert item["prompt"]
            assert "metadata" in item

    async def test_detail_response_includes_heavy_columns(self, client, seeded):
        response = await client.get(f"/api/tasks/{seeded['detail_id']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["messages_json"] == _BIG_MESSAGES
        assert body["tool_calls"] == _BIG_TOOL_CALLS

    async def test_running_task_still_listed(self, client, seeded):
        response = await client.get("/api/tasks", params={"agent_id": seeded["agent_id"]})
        assert response.status_code == 200, response.text
        statuses = {item["id"]: item["status"] for item in response.json()}
        assert statuses.get(seeded["running_id"]) == "running"

    async def test_runtime_ledger_works_with_deferred_columns(self, client, seeded):
        response = await client.get("/api/runtime-ledger", params={"agent_id": seeded["agent_id"]})
        assert response.status_code == 200, response.text
        entries = response.json()["entries"]
        request_ids = {entry["id"] for entry in entries if entry["entry_type"] == "request"}
        assert f"task-request:{seeded['detail_id']}" in request_ids
        for entry in entries:
            assert "messages_json" not in entry
            assert "tool_calls" not in entry
