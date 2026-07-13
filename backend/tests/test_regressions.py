import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Response

from hermeshq.core.security import create_access_token, get_current_user
from hermeshq.database import init_database
from hermeshq.models.user import User
from hermeshq.routers.auth import logout
from hermeshq.services.pty_manager import PTYManager
from hermeshq.services.scheduler import SchedulerService


class _FakeSession:
    def __init__(self, user: User) -> None:
        self.user = user

    async def get(self, model, subject):
        if model is User and subject == self.user.id:
            return self.user
        return None


class AuthRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_current_user_accepts_cookie_without_bearer_header(self) -> None:
        user = User(
            id="user-1",
            username="operator",
            display_name="Operator",
            password_hash="hashed",
            role="admin",
            is_active=True,
        )
        token, _ = create_access_token(user.id, subject_kind="id")

        resolved = await get_current_user(
            bearer_token=None,
            cookie_token=token,
            db=_FakeSession(user),
        )

        self.assertEqual(resolved.id, user.id)

    async def test_logout_clears_cookie_on_returned_response(self) -> None:
        user = User(
            id="user-1",
            username="operator",
            display_name="Operator",
            password_hash="hashed",
            role="admin",
            is_active=True,
        )
        response = Response()

        returned = await logout(response=response, current_user=user)

        self.assertIs(returned, response)
        self.assertEqual(returned.status_code, 204)
        cookie_header = returned.headers.get("set-cookie", "")
        self.assertIn("hermeshq_token=", cookie_header)
        self.assertIn("Max-Age=0", cookie_header)


class DatabaseInitRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_database_propagates_alembic_failures(self) -> None:
        # init_database() now detects DB state before running migrations.
        # Mock _detect_db_state to return "stamped" so it proceeds to subprocess.
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stderr = "migration failed"
        mock_result.stdout = ""
        with patch("hermeshq.database._detect_db_state", new=AsyncMock(return_value="stamped")), patch(
            "subprocess.run",
            return_value=mock_result,
        ):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                await init_database()


class SchedulerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_skips_overlapping_execution_in_same_process(self) -> None:
        scheduler = SchedulerService(session_factory=Mock(), on_task_created=AsyncMock())
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_tick_once() -> None:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        scheduler._tick_once = fake_tick_once  # type: ignore[method-assign]

        first = asyncio.create_task(scheduler.tick())
        await started.wait()
        await scheduler.tick()
        release.set()
        await first

        self.assertEqual(calls, 1)


class PTYManagerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_serializes_concurrent_requests_per_agent(self) -> None:
        manager = PTYManager("/bin/sh", audit_callback=AsyncMock())
        read_fd, write_fd = os.pipe()
        process = Mock(pid=1234)
        process.poll.return_value = 0
        process.wait.return_value = 0

        async def fake_reader_loop(_session) -> None:
            return None

        with patch("hermeshq.services.pty_manager.pty.openpty", return_value=(read_fd, write_fd)), patch(
            "hermeshq.services.pty_manager.subprocess.Popen",
            return_value=process,
        ) as popen, patch.object(manager, "_resize_fd", return_value=None), patch.object(
            manager,
            "_reader_loop",
            side_effect=fake_reader_loop,
        ):
            first, second = await asyncio.gather(
                manager.create_session("agent-1", "hybrid", "/tmp", command=["echo"]),
                manager.create_session("agent-1", "hybrid", "/tmp", command=["echo"]),
            )

        self.assertIs(first, second)
        self.assertEqual(popen.call_count, 1)
        await manager.destroy_session("agent-1")


if __name__ == "__main__":
    unittest.main()
