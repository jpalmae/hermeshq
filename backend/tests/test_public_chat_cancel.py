import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, PublicChatService, SimpleRateLimiter


def _task(task_id, session_id="s1", user_id="u1", source="public_chat", status="running"):
    return SimpleNamespace(
        id=task_id,
        status=status,
        completed_at=None,
        board_column=None,
        metadata_json={"source": source, "public_session_id": session_id, "thread_user_id": user_id},
    )


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


def _service(tasks, active=("t1",)):
    service = PublicChatService.__new__(PublicChatService)
    service.rate_limiter = SimpleRateLimiter(max_requests=100)
    service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
    service.supervisor = SimpleNamespace(active_tasks={tid: object() for tid in active}, cancel_task=AsyncMock())
    session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None)
    service.validate_session = AsyncMock(return_value=session)
    service._resolve_identity = AsyncMock(return_value=("u1", None))
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id="k1"))
    db.execute = AsyncMock(return_value=_Result(tasks))
    db.commit = AsyncMock()
    db.add = MagicMock()
    return service, db


class TestHandleCancelRequest(unittest.IsolatedAsyncioTestCase):
    async def test_not_a_cancel_returns_none_and_touches_nothing(self):
        service, db = _service([_task("t1")])
        self.assertIsNone(await service.handle_cancel_request("s1", "tok", "dame el html", db))
        db.execute.assert_not_called()
        service.supervisor.cancel_task.assert_not_called()

    async def test_cancel_stops_the_running_task_of_this_chat(self):
        service, db = _service([_task("t1")])
        reply = await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")
        self.assertIn("cancelé", reply)
        self.assertEqual(db.add.call_count, 2)

    async def test_other_users_task_on_the_same_agent_is_left_alone(self):
        service, db = _service([_task("t1", session_id="otra", user_id="u2")])
        reply = await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()
        self.assertIn("No tengo ninguna tarea", reply)

    async def test_task_of_an_older_session_of_the_same_user_is_cancelled(self):
        service, db = _service([_task("t1", session_id="vieja", user_id="u1")])
        await service.handle_cancel_request("s1", "tok", "stop", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")

    async def test_anonymous_user_only_cancels_its_own_session(self):
        service, db = _service([_task("t1", session_id="otra", user_id=PUBLIC_CHAT_USER_ID)])
        service._resolve_identity = AsyncMock(return_value=(PUBLIC_CHAT_USER_ID, None))
        await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_non_chat_tasks_are_never_cancelled(self):
        service, db = _service([_task("t1", source="scheduled")])
        await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_queued_task_without_runner_is_marked_cancelled_directly(self):
        queued = _task("t2", status="queued")
        service, db = _service([queued], active=())
        await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()
        self.assertEqual(queued.status, "cancelled")
        self.assertIsNotNone(queued.completed_at)

    async def test_two_tasks_give_plural_reply(self):
        service, db = _service([_task("t1"), _task("t3")], active=("t1", "t3"))
        reply = await service.handle_cancel_request("s1", "tok", "para", db)
        self.assertIn("2 tareas", reply)
        self.assertEqual(service.supervisor.cancel_task.await_count, 2)
