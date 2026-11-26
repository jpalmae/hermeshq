import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.models.base import utcnow
from hermeshq.routers.public_chat import _friendly_task_error
from hermeshq.services import auto_background
from hermeshq.services.cancel_intent import is_ack_request
from hermeshq.services.public_chat_service import PublicChatService

TIMEOUT = "Hermes runtime exceeded the task timeout of 870s"


def _task(**metadata):
    base = {"source": "public_chat", "thread_user_id": "u1", "public_session_id": "s1", "conversation": True}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", prompt="si vamos con el informe", metadata_json=base)


def _session(activos=(), mensajes=(), usuario=True):
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id="u1", is_active=True) if usuario else None)
    historial = MagicMock()
    historial.scalars.return_value.all.return_value = list(mensajes)
    pendientes = MagicMock()
    pendientes.scalars.return_value.all.return_value = list(activos)
    session.execute = AsyncMock(side_effect=[pendientes, historial])
    session.add = MagicMock()
    session.flush = AsyncMock(side_effect=lambda: None)
    return session


class TestIsContinuable(unittest.TestCase):
    def test_only_chat_timeouts_qualify(self):
        self.assertTrue(auto_background.is_continuable(_task(), TIMEOUT))
        self.assertFalse(auto_background.is_continuable(_task(), "produced no output for 300s"))
        self.assertFalse(auto_background.is_continuable(_task(background_job=True), TIMEOUT))
        self.assertFalse(auto_background.is_continuable(_task(scheduled=True), TIMEOUT))
        self.assertFalse(auto_background.is_continuable(_task(source="web"), TIMEOUT))
        self.assertFalse(auto_background.is_continuable(_task(thread_user_id=""), TIMEOUT))
        self.assertFalse(auto_background.is_continuable(_task(public_chat_binding_denied="x"), TIMEOUT))


class TestCreateContinuation(unittest.IsolatedAsyncioTestCase):
    async def _crear(self, task, session):
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            return await auto_background.create_continuation(session, task, TIMEOUT)

    async def test_it_creates_a_background_job_with_context_and_marks_the_task(self):
        mensajes = [
            SimpleNamespace(role="user", content="necesito un informe de incidentes"),
            SimpleNamespace(role="assistant", content="Propongo 7 pasos"),
        ]
        task = _task()
        session = _session(mensajes=mensajes)
        job = await self._crear(task, session)
        self.assertIsNotNone(job)
        self.assertTrue(job.metadata_json["background_job"])
        self.assertTrue(job.metadata_json["auto_continuation"])
        self.assertEqual(job.metadata_json["thread_user_id"], "u1")
        self.assertEqual(job.metadata_json["origin_task_id"], "t1")
        self.assertNotIn("conversation", job.metadata_json)
        self.assertIn("Usuario: necesito un informe de incidentes", job.prompt)
        self.assertIn("Tú: Propongo 7 pasos", job.prompt)
        self.assertIn("si vamos con el informe", job.prompt)
        self.assertIn("60 minutos", job.prompt)
        self.assertEqual(task.metadata_json[auto_background.AUTO_MARKER], job.id)

    async def test_history_skips_system_replies_and_keeps_only_the_last_messages(self):
        largo = "necesito el informe interactivo del procedimiento de incidentes con roles y pasos " * 2
        recientes = [SimpleNamespace(role="user", content="genera el entregable")]
        recientes += [SimpleNamespace(role="assistant", content="⚠️ Mi intento anterior se agotó")]
        recientes += [SimpleNamespace(role="user", content=f"ok {i}") for i in range(auto_background.HISTORY_MESSAGES)]
        recientes += [SimpleNamespace(role="user", content=largo)]
        sesion = MagicMock()
        resultado = MagicMock()
        resultado.scalars.return_value.all.return_value = recientes
        sesion.execute = AsyncMock(return_value=resultado)
        historial = await auto_background._recent_history(sesion, "s1")
        self.assertNotIn("Mi intento anterior", historial)
        self.assertNotIn("Pedido de fondo anterior", historial)
        self.assertEqual(len(historial.splitlines()), auto_background.HISTORY_MESSAGES)

    async def test_recent_history_cuts_at_the_most_recent_reset(self):
        msgs = [
            SimpleNamespace(role="user", content="dale"),
            SimpleNamespace(
                role="assistant", content="🧹 Listo, conversación reiniciada: empezamos de cero. ¿Qué necesitas?"
            ),
            SimpleNamespace(role="user", content="/reset"),
            SimpleNamespace(role="assistant", content="ok, preparo el informe de Norte"),
            SimpleNamespace(
                role="user",
                content="necesito el informe de incidentes y accidentes de Norte de los ultimos 5 anos",
            ),
        ]
        sesion = MagicMock()
        resultado = MagicMock()
        resultado.scalars.return_value.all.return_value = msgs
        sesion.execute = AsyncMock(return_value=resultado)
        historial = await auto_background._recent_history(sesion, "s1")
        self.assertIn("dale", historial)
        self.assertNotIn("Norte", historial)
        self.assertNotIn("informe de incidentes", historial)
        self.assertNotIn("/reset", historial)

    async def test_without_a_theme_after_the_reset_it_asks_to_restate_instead_of_reusing(self):
        msgs = [
            SimpleNamespace(role="user", content="dale"),
            SimpleNamespace(
                role="assistant", content="🧹 Listo, conversación reiniciada: empezamos de cero. ¿Qué necesitas?"
            ),
            SimpleNamespace(role="user", content="/reset"),
            SimpleNamespace(
                role="user",
                content="necesito el informe de incidentes y accidentes de Norte de los ultimos 5 anos",
            ),
        ]
        task = _task()
        task.prompt = "dale"
        session = _session(mensajes=msgs)
        job = await self._crear(task, session)
        self.assertIsNotNone(job)
        self.assertNotIn("reutiliza", job.prompt)
        self.assertNotIn("Norte", job.prompt)
        self.assertIn("vuelva a indicar", job.prompt)

    async def test_with_a_theme_it_keeps_reusing_only_the_matching_artifacts(self):
        msgs = [SimpleNamespace(role="user", content="necesito un informe de incidentes de la planta de Norte")]
        task = _task()
        session = _session(mensajes=msgs)
        job = await self._crear(task, session)
        self.assertIsNotNone(job)
        self.assertIn("reutiliza solo", job.prompt)
        self.assertIn("ignora cualquier archivo de un tema distinto", job.prompt)

    async def test_no_second_job_while_the_user_already_has_one(self):
        activo = SimpleNamespace(metadata_json={"background_job": True, "thread_user_id": "u1"})
        session = _session(activos=[activo])
        self.assertIsNone(await self._crear(_task(), session))
        session.add.assert_not_called()

    async def test_other_users_jobs_do_not_block(self):
        otro = SimpleNamespace(metadata_json={"background_job": True, "thread_user_id": "u2"})
        self.assertIsNotNone(await self._crear(_task(), _session(activos=[otro])))

    async def test_inactive_or_unknown_users_get_nothing(self):
        self.assertIsNone(await self._crear(_task(), _session(usuario=False)))

    async def test_errors_that_are_not_timeouts_are_ignored(self):
        session = _session()
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            self.assertIsNone(await auto_background.create_continuation(session, _task(), "invalid tool call"))


class TestTitle(unittest.TestCase):
    def test_the_attachment_note_is_not_part_of_the_title(self):
        prompt = "[Archivos adjuntos, ya disponibles en tu workspace: - guion.txt (6 KB) -> uploads/a.txt] Trabajemos en la Skill 2"
        self.assertEqual(auto_background._title_from(prompt), "Trabajemos en la Skill 2")
        self.assertEqual(auto_background._title_from("Sin adjuntos"), "Sin adjuntos")
        solo = "[Archivos adjuntos, ya disponibles: - a.pdf -> uploads/a.pdf]"
        self.assertEqual(auto_background._title_from(solo), "solicitud con archivos adjuntos")


class TestFriendlyMessageAndAck(unittest.TestCase):
    def test_the_user_is_told_it_continues_in_the_background(self):
        texto = _friendly_task_error({"error": TIMEOUT, "auto_background": True})
        self.assertIn("segundo plano", texto)
        self.assertIn("te aviso", texto)
        self.assertIn("demasiado", _friendly_task_error({"error": TIMEOUT}))

    def test_short_acknowledgements_are_detected(self):
        for frase in ("ok", "Arraca", "dale ya", "sí", "continúa"):
            self.assertTrue(is_ack_request(frase), frase)
        for frase in ("ok pero hazlo en excel", "", "genera el informe", "hace una prueba"):
            self.assertFalse(is_ack_request(frase), frase)


class TestFailedAttemptReply(unittest.IsolatedAsyncioTestCase):
    def _servicio(self):
        return PublicChatService.__new__(PublicChatService)

    def _db(self, tareas):
        db = MagicMock()
        resultado = MagicMock()
        resultado.scalars.return_value.all.return_value = tareas
        db.execute = AsyncMock(return_value=resultado)
        return db

    def _fallida(self, minutos=5, error=TIMEOUT):
        metadata = {"conversation": True, "source": "public_chat", "public_session_id": "s1", "thread_user_id": "u1"}
        return SimpleNamespace(
            status="failed",
            completed_at=utcnow() - timedelta(minutes=minutos),
            error_message=error,
            prompt="y el informe?",
            metadata_json=metadata,
        )

    async def test_it_explains_a_recent_timeout_instead_of_rerunning_the_agent(self):
        servicio = self._servicio()
        respuesta = await servicio._failed_attempt_reply(
            self._db([self._fallida()]), SimpleNamespace(agent_id="a1"), "s1", "u1"
        )
        self.assertIn("hazlo en segundo plano", respuesta)

    async def test_old_or_other_failures_return_nothing(self):
        servicio = self._servicio()
        sesion = SimpleNamespace(agent_id="a1")
        self.assertIsNone(
            await servicio._failed_attempt_reply(self._db([self._fallida(minutos=120)]), sesion, "s1", "u1")
        )
        self.assertIsNone(
            await servicio._failed_attempt_reply(self._db([self._fallida(error="boom")]), sesion, "s1", "u1")
        )
        self.assertIsNone(await servicio._failed_attempt_reply(self._db([]), sesion, "s1", "u1"))


if __name__ == "__main__":
    unittest.main()
