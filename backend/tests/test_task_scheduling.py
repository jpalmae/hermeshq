import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermeshq.config import Settings
from hermeshq.routers.public_chat import _friendly_task_error
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.hermes_runtime import HermesRuntime


def _supervisor(slots=2):
    sup = AgentSupervisor.__new__(AgentSupervisor)
    sup._agent_locks = {}
    sup.cancelaciones_pedidas = set()
    sup._configurar_admision(slots)
    sup.active_tasks = {}
    return sup


class TestPublicChatDeadlines(unittest.TestCase):
    def _timeout(self, source, **overrides):
        settings = Settings(debug=True, jwt_secret="x" * 40, **overrides)
        task = SimpleNamespace(metadata_json={"source": source})
        with patch("hermeshq.config.get_settings", return_value=settings):
            return HermesRuntime._task_timeout_for(task)

    def test_public_chat_gets_shorter_deadline(self):
        self.assertEqual(self._timeout("public_chat"), 540)

    def test_other_sources_keep_global_deadline(self):
        self.assertEqual(self._timeout("api"), 3600)

    def test_zero_disables_public_chat_deadline(self):
        self.assertEqual(self._timeout("public_chat", public_chat_task_timeout_seconds=0), 3600)

    def test_never_exceeds_global_deadline(self):
        self.assertEqual(self._timeout("public_chat", task_timeout_seconds=300), 300)


class TestFriendlyErrors(unittest.TestCase):
    def test_queue_timeout(self):
        msg = _friendly_task_error({"error_type": "QueueTimeout"})
        self.assertNotIn("ocupados", msg)
        self.assertIn("sigue en curso", msg)
        self.assertIn("no se procesó", msg)
        self.assertIn("cancela", msg)

    def test_runtime_timeout(self):
        msg = _friendly_task_error({"error": "Hermes runtime exceeded the task timeout of 540s"})
        self.assertIn("tardó demasiado", msg)

    def test_idle_timeout(self):
        msg = _friendly_task_error({"error": "Hermes runtime produced no output for 600s and was stopped"})
        self.assertIn("tardó demasiado", msg)

    def test_other_errors_stay_generic(self):
        msg = _friendly_task_error({"error": "boom"})
        self.assertIn("Hubo un problema al procesar tu solicitud", msg)
        self.assertNotIn("The agent encountered", msg)

    def test_unused(self):
        self.assertIsNotNone(MagicMock)
