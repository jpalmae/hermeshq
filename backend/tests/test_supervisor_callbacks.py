"""Tests for AgentSupervisor._drain_callbacks — post-commit callback isolation."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.agent_supervisor import AgentSupervisor, _ResizableLimiter, _StreamBuffer


def _make_supervisor() -> AgentSupervisor:
    return AgentSupervisor(
        session_factory=MagicMock(),
        event_broker=MagicMock(),
        runtime=MagicMock(),
        secret_vault=MagicMock(),
    )


class TestDrainCallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_runs_all_callbacks_in_order(self) -> None:
        supervisor = _make_supervisor()
        calls: list[str] = []

        async def cb_a() -> None:
            calls.append("a")

        async def cb_b() -> None:
            calls.append("b")

        await supervisor._drain_callbacks([cb_a, cb_b])
        self.assertEqual(calls, ["a", "b"])

    async def test_failing_callback_does_not_abort_rest(self) -> None:
        supervisor = _make_supervisor()
        calls: list[str] = []

        async def failing() -> None:
            raise RuntimeError("telegram is down")

        async def after() -> None:
            calls.append("after")

        await supervisor._drain_callbacks([failing, after])
        self.assertEqual(calls, ["after"])

    async def test_empty_list_is_noop(self) -> None:
        supervisor = _make_supervisor()
        await supervisor._drain_callbacks([])

    async def test_mixed_sync_exceptions_are_contained(self) -> None:
        supervisor = _make_supervisor()
        cb = AsyncMock(side_effect=ValueError("boom"))
        await supervisor._drain_callbacks([cb])
        cb.assert_awaited_once()


class TestResizableLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_shrink_waits_for_existing_work_to_drain(self) -> None:
        limiter = _ResizableLimiter(2)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.resize(1)
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        await limiter.release()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        await limiter.release()
        await asyncio.wait_for(waiter, timeout=1)
        await limiter.release()


class TestStreamBuffer(unittest.IsolatedAsyncioTestCase):
    async def test_failed_flush_requeues_deltas(self) -> None:
        class FailingSessionContext:
            async def __aenter__(self):
                raise RuntimeError("database unavailable")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        buffer = _StreamBuffer(
            session_factory=lambda: FailingSessionContext(),
            task_id="task-1",
            agent_id="agent-1",
            log_func=AsyncMock(),
            event_broker=MagicMock(),
        )
        buffer._deltas = [("hello", 1)]
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await buffer.flush()
        self.assertEqual(buffer._deltas, [("hello", 1)])


class TestConversationHistory(unittest.IsolatedAsyncioTestCase):
    async def test_only_completed_turns_enter_model_context(self) -> None:
        supervisor = _make_supervisor()
        thread_id = "thread-1"

        def task(task_id: str, status: str, prompt: str, response: str = ""):
            value = MagicMock()
            value.id = task_id
            value.status = status
            value.prompt = prompt
            value.response = response
            value.messages_json = []
            value.error_message = "internal stack trace" if status == "failed" else None
            value.metadata_json = {"conversation": True, "thread_id": thread_id}
            return value

        completed = task("completed", "completed", "first", "answer")
        failed = task("failed", "failed", "second")
        cancelled = task("cancelled", "cancelled", "third")
        current = task("current", "queued", "fourth")
        result = MagicMock()
        result.scalars.return_value.all.return_value = [current, cancelled, failed, completed]
        session = AsyncMock()
        session.execute.return_value = result

        history = await supervisor._build_conversation_history(session, current)

        self.assertEqual(
            history,
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
