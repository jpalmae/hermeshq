import asyncio
import unittest
from unittest.mock import patch

from hermeshq.config import Settings
from hermeshq.services.hermes_runtime import HermesRuntime, RuntimeExecutionError


class TestIdleWatchdog(unittest.IsolatedAsyncioTestCase):
    async def test_silent_stream_is_stopped(self):
        reader = asyncio.StreamReader()
        with self.assertRaises(RuntimeExecutionError) as ctx:
            await HermesRuntime._readline_or_stall(reader, 1)
        self.assertIn("no output for 1s", str(ctx.exception))

    async def test_line_is_returned_when_available(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"event": "delta"}\n')
        self.assertEqual(await HermesRuntime._readline_or_stall(reader, 1), b'{"event": "delta"}\n')

    async def test_eof_returns_empty(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        self.assertEqual(await HermesRuntime._readline_or_stall(reader, 1), b"")

    async def test_zero_disables_idle_limit(self):
        reader = asyncio.StreamReader()

        async def feed():
            await asyncio.sleep(0.1)
            reader.feed_data(b"x\n")

        feeder = asyncio.create_task(feed())
        self.assertEqual(await HermesRuntime._readline_or_stall(reader, None), b"x\n")
        await feeder


class TestTimeoutEnv(unittest.TestCase):
    def _env(self, **overrides):
        settings = Settings(debug=True, jwt_secret="x" * 40, **overrides)
        with patch("hermeshq.config.get_settings", return_value=settings):
            return HermesRuntime._model_timeout_env(), HermesRuntime._task_idle_timeout_seconds()

    def test_defaults_are_tighter_than_hermes_defaults(self):
        env, idle = self._env()
        self.assertEqual(env["HERMES_API_TIMEOUT"], "360")
        self.assertEqual(env["HERMES_STREAM_READ_TIMEOUT"], "120")
        self.assertEqual(env["HERMES_STREAM_STALE_TIMEOUT"], "180")
        self.assertEqual(env["HERMES_API_CALL_STALE_TIMEOUT"], "240")
        self.assertEqual(env["HERMES_STREAM_RETRIES"], "1")
        self.assertEqual(idle, 600)

    def test_floor_applies(self):
        env, _ = self._env(model_call_timeout_seconds=5)
        self.assertEqual(env["HERMES_STREAM_READ_TIMEOUT"], "30")

    def test_idle_zero_disables(self):
        _, idle = self._env(task_idle_timeout_seconds=0)
        self.assertEqual(idle, 0)
