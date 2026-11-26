import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.hermes_runtime import PLATFORM_RULES
from hermeshq.services.schedule_notifications import enqueue_handoff_notice


def _session(user, keys):
    session = MagicMock()
    session.get = AsyncMock(return_value=user)
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = keys
    session.execute = AsyncMock(return_value=resultado)
    session.add = MagicMock()
    return session


def _key():
    return SimpleNamespace(
        id="k1",
        trust_sender_id=False,
        bound_user_id="u1",
        last_notification_poll_at=datetime.now(UTC) - timedelta(seconds=20),
    )


def _task(**metadata):
    base = {"source": "public_chat", "thread_user_id": "u1", "conversation": True}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", status="failed", metadata_json=base)


class TestHandoffNotice(unittest.IsolatedAsyncioTestCase):
    async def test_a_timed_out_chat_task_tells_the_user_it_continues_in_background(self):
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [_key()])
        cantidad = await enqueue_handoff_notice(sesion, _task(), "informe SGSI-PRO-INC-01")
        self.assertEqual(cantidad, 1)
        aviso = sesion.add.call_args.args[0]
        self.assertIn("segundo plano", aviso.body)
        self.assertIn("informe SGSI-PRO-INC-01", aviso.body)
        self.assertIn("No hace falta reenviarla", aviso.body)
        self.assertEqual(aviso.task_id, "t1")
        self.assertIsNone(aviso.schedule_id)

    async def test_background_and_scheduled_tasks_get_no_handoff_notice(self):
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [_key()])
        for metadata in ({"background_job": True}, {"scheduled": True}, {"thread_user_id": ""}):
            self.assertEqual(await enqueue_handoff_notice(sesion, _task(**metadata), "x"), 0)
        sesion.add.assert_not_called()

    async def test_without_a_title_it_falls_back_to_a_generic_wording(self):
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [_key()])
        self.assertEqual(await enqueue_handoff_notice(sesion, _task(), None), 1)
        self.assertIn("tu solicitud", sesion.add.call_args.args[0].body)


class TestShortContinuationRule(unittest.TestCase):
    def test_platform_rules_cover_short_continuation_commands(self):
        for fragmento in ("ejecuta", "dale", "¿Ejecuto", "última acción concreta", "en vez de "):
            self.assertIn(fragmento, PLATFORM_RULES)


class TestCalendarAttendeesRule(unittest.TestCase):
    def test_platform_rules_forbid_inventing_attendees(self):
        for fragmento in (
            "nunca inventes ni adivines correos ni asistentes",
            "primero LEE la invitación",
            "aunque sean externos",
            "crea el evento SOLO con el usuario",
            "roster «Known agents» es solo",
            "muestra la lista exacta de destinatarios",
        ):
            self.assertIn(fragmento, PLATFORM_RULES)


if __name__ == "__main__":
    unittest.main()
