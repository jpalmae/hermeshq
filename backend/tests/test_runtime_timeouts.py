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


class TestPresupuestoDelChatConLaCola(unittest.TestCase):
    def _task(self, cola_s, fuente="public_chat", fondo=False):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        inicio = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
        return SimpleNamespace(
            metadata_json={"source": fuente, **({"background_job": True} if fondo else {})},
            queued_at=inicio,
            started_at=inicio + timedelta(seconds=cola_s),
        )

    def _timeout(self, task):
        settings = Settings(debug=True, jwt_secret="x" * 40)
        with patch("hermeshq.config.get_settings", return_value=settings):
            return HermesRuntime._task_timeout_for(task)

    def test_sin_espera_el_chat_conserva_540_segundos(self):
        self.assertEqual(self._timeout(self._task(0)), 540)

    def test_con_240_segundos_de_cola_quedan_330_y_la_suma_no_pasa_de_600(self):
        t = self._timeout(self._task(240))
        self.assertEqual(t, 330)
        self.assertLess(240 + t, 600)

    def test_con_mucha_cola_queda_el_minimo_de_60(self):
        self.assertEqual(self._timeout(self._task(540)), 60)

    def test_control_negativo_un_trabajo_en_segundo_plano_no_se_recorta_por_la_cola(self):
        self.assertEqual(self._timeout(self._task(240, fondo=True)), 3600)

    def test_control_negativo_otras_fuentes_no_se_recortan(self):
        self.assertEqual(self._timeout(self._task(240, fuente="web")), 3600)

    def test_sin_marcas_de_tiempo_no_descuenta(self):
        from types import SimpleNamespace

        t = SimpleNamespace(metadata_json={"source": "public_chat"}, queued_at=None, started_at=None)
        self.assertEqual(self._timeout(t), 540)
