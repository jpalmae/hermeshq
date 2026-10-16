import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.models.base import utcnow
from hermeshq.services.agent_supervisor import HISTORY_USER_MARKER, AgentSupervisor
from hermeshq.services.cancel_intent import is_reset_request, is_status_request, split_reset_prefix
from hermeshq.services.public_chat_service import PublicChatService


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


class _Session:
    def __init__(self, items):
        self.items = items

    async def execute(self, _query):
        return _Result(self.items)


def _turn(idx, hours_ago, status="completed", response="ok"):
    return SimpleNamespace(
        id=f"t{idx}",
        prompt=f"pregunta {idx}",
        status=status,
        response=response,
        error_message=None,
        messages_json=[],
        queued_at=utcnow() - timedelta(hours=hours_ago),
        metadata_json={"conversation": True},
    )


class TestResetBoundary(unittest.IsolatedAsyncioTestCase):
    async def _history(self, items):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        current = SimpleNamespace(id="now", metadata_json={"conversation": True}, agent_id="a")
        settings = SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            return await supervisor._build_conversation_history(_Session(list(reversed(items))), current)

    async def test_a_reset_drops_everything_before_it(self):
        reset = _turn(3, 1)
        reset.prompt = "/reset"
        reset.metadata_json = {"conversation": True, "reset": True}
        history = await self._history([_turn(1, 2), _turn(2, 2), reset])
        self.assertEqual(history, [])
        history = await self._history([_turn(1, 2), reset, _turn(4, 1)])
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], [HISTORY_USER_MARKER + "pregunta 4"])


class TestResetCommands(unittest.TestCase):
    def test_reset_commands_are_recognised(self):
        for texto in ("/reset", " /Nuevo ", "reset", "/limpiar."):
            self.assertTrue(is_reset_request(texto), texto)
        for texto in ("resetea el informe de ventas", "hola", "", None):
            self.assertFalse(is_reset_request(texto), texto)

    def test_a_reset_prefix_keeps_the_rest_of_the_message(self):
        self.assertEqual(
            split_reset_prefix("/new  Tomando como base el informe V2, crea V3"),
            "Tomando como base el informe V2, crea V3",
        )
        self.assertEqual(split_reset_prefix("/nuevo: haz un resumen"), "haz un resumen")
        self.assertIsNone(split_reset_prefix("/reset"))
        self.assertIsNone(split_reset_prefix("hola /new cosa"))
        self.assertIsNone(split_reset_prefix("/newsletter semanal"))
        self.assertIsNone(split_reset_prefix(None))


class TestStatusRequests(unittest.TestCase):
    def test_short_status_questions_are_recognised(self):
        for texto in (
            "¿Cómo va?",
            "que paso? por qué demoras tanto? necesito el entregable",
            "Y el informe?",
            "y la pptx?",
            "no obtuve respuesta",
            "estado de mi solicitud",
        ):
            self.assertTrue(is_status_request(texto), texto)
        for texto in (
            "cómo va el proyecto de licencias de power bi en todas las sucursales y qué pasó con los costos del año",
            "genera el informe de Arica",
            "estado",
            "hola",
            "",
            None,
        ):
            self.assertFalse(is_status_request(texto), texto)

    def test_the_reply_says_how_long_it_has_been_running_and_what_is_running(self):
        ahora = utcnow()
        corriendo = SimpleNamespace(
            status="running",
            started_at=ahora - timedelta(seconds=185),
            queued_at=ahora,
            prompt="genera el informe de Arica " * 10,
        )
        espera = SimpleNamespace(status="queued", started_at=None, queued_at=ahora, prompt="y el informe?")
        texto = PublicChatService._status_reply([corriendo, espera])
        self.assertIn("3 min", texto)
        self.assertIn("genera el informe de Arica", texto)
        self.assertIn("1 mensaje(s) más", texto)
        self.assertIn("cancela", texto)
        solo_espera = PublicChatService._status_reply([espera])
        self.assertIn("en cola", solo_espera)


if __name__ == "__main__":
    unittest.main()
