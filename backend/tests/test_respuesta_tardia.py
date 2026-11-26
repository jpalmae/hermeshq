import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.models.public_chat import PublicChatNotification
from hermeshq.services.schedule_notifications import enqueue_respuesta_tardia


def _task(**metadata):
    base = {"thread_user_id": "u1", "respuesta_tardia": True}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", metadata_json=base)


def _session(user=None):
    session = MagicMock()
    session.get = AsyncMock(return_value=user or SimpleNamespace(id="u1", is_active=True, teams_id=None))
    session.add = MagicMock()
    return session


KEY = SimpleNamespace(id="k1", trust_sender_id=False)


class RespuestaTardia(unittest.IsolatedAsyncioTestCase):
    async def test_sin_la_marca_no_se_envia_nada(self):
        session = _session()
        task = _task(respuesta_tardia=False)
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            self.assertEqual(await enqueue_respuesta_tardia(session, task, success=True, summary="hola"), 0)
        session.add.assert_not_called()

    async def test_con_la_marca_la_respuesta_llega_como_notificacion(self):
        session = _session()
        task = _task()
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            count = await enqueue_respuesta_tardia(session, task, success=True, summary="Informe listo.")
        self.assertEqual(count, 1)
        notificacion = session.add.call_args.args[0]
        self.assertIsInstance(notificacion, PublicChatNotification)
        self.assertEqual(notificacion.title, "Respuesta a tu solicitud")
        self.assertEqual(notificacion.body, "Informe listo.")
        self.assertEqual(notificacion.task_id, "t1")

    async def test_las_lineas_media_no_llegan_a_teams(self):
        session = _session()
        task = _task()
        resumen = "Listo.\nMEDIA: uploads/a.html"
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            await enqueue_respuesta_tardia(session, task, success=True, summary=resumen)
        self.assertNotIn("MEDIA:", session.add.call_args.args[0].body)

    async def test_una_tabla_llega_como_tabla_markdown(self):
        session = _session()
        task = _task()
        tabla = "| Prioridad | ID |\n|---|---|\n| CRÍTICO | 54 |"
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            await enqueue_respuesta_tardia(session, task, success=True, summary=tabla)
        self.assertIn("| CRÍTICO | 54 |", session.add.call_args.args[0].body)
        self.assertNotIn("```", session.add.call_args.args[0].body)

    async def test_un_fallo_tambien_avisa_al_usuario(self):
        session = _session()
        task = _task()
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            count = await enqueue_respuesta_tardia(session, task, success=False, summary="timeout")
        self.assertEqual(count, 1)
        self.assertTrue(session.add.call_args.args[0].body.startswith("No pude completar tu solicitud"))

    async def test_usuario_inactivo_no_recibe_nada(self):
        session = _session(SimpleNamespace(id="u1", is_active=False, teams_id=None))
        with patch("hermeshq.services.schedule_notifications._delivery_keys", AsyncMock(return_value=[KEY])):
            self.assertEqual(await enqueue_respuesta_tardia(session, _task(), success=True, summary="x"), 0)
        session.add.assert_not_called()
