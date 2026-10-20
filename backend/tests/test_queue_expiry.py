import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.models.base import utcnow
from hermeshq.services.agent_supervisor import AgentSupervisor


def _task(task_id, source, minutes_ago):
    return SimpleNamespace(
        id=task_id,
        agent_id="agent-1",
        title="t",
        status="queued",
        queued_at=utcnow() - timedelta(minutes=minutes_ago),
        completed_at=None,
        error_message=None,
        created_by_user_id="user-1",
        metadata_json={"source": source},
    )


class _Session:
    def __init__(self, tasks):
        result = MagicMock()
        result.scalars.return_value.all.return_value = tasks
        self.execute = AsyncMock(return_value=result)
        self.get = AsyncMock(return_value=SimpleNamespace(id="agent-1"))
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _supervisor(session):
    sup = AgentSupervisor.__new__(AgentSupervisor)
    sup.session_factory = lambda: session
    sup.event_broker = SimpleNamespace(publish=AsyncMock())
    sup._log = AsyncMock()
    return sup


class QueueExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tasks, timeout=240):
        session = _Session(tasks)
        sup = _supervisor(session)
        settings = SimpleNamespace(public_chat_queue_timeout_seconds=timeout)
        with (
            patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings),
            patch("hermeshq.services.agent_supervisor.sync_board_with_runtime", new=AsyncMock()),
        ):
            await sup._expire_stale_queued_tasks()
        return sup, session

    async def test_stale_chat_task_is_failed_and_announced(self):
        task = _task("t1", "public_chat", 10)
        sup, session = await self._run([task])
        self.assertEqual(task.status, "failed")
        self.assertIn("240s", task.error_message)
        event = sup.event_broker.publish.await_args.args[0]
        self.assertEqual(event["type"], "task.failed")
        self.assertEqual(event["error_type"], "QueueTimeout")
        session.commit.assert_awaited_once()

    async def test_other_sources_are_never_expired(self):
        task = _task("t2", "scheduler", 600)
        sup, session = await self._run([task])
        self.assertEqual(task.status, "queued")
        sup.event_broker.publish.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_zero_disables_expiry(self):
        session = _Session([_task("t3", "public_chat", 600)])
        sup = _supervisor(session)
        settings = SimpleNamespace(public_chat_queue_timeout_seconds=0)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            await sup._expire_stale_queued_tasks()
        session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
