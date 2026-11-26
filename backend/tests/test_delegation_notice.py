import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.schedule_notifications import enqueue_delegation_notice


def _key():
    return SimpleNamespace(
        id="k1",
        trust_sender_id=False,
        bound_user_id="u1",
        last_notification_poll_at=datetime.now(UTC) - timedelta(seconds=20),
    )


def _session(owner, agent_name, keys):
    session = MagicMock()

    def get(modelo, _id):
        name = modelo.__name__
        if name == "User":
            return owner
        if name == "Agent":
            return SimpleNamespace(id=_id, name=agent_name)
        return None

    session.get = AsyncMock(side_effect=get)
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = keys
    session.execute = AsyncMock(return_value=resultado)
    session.add = MagicMock()
    return session


def _task(**metadata):
    base = {"delegated": True, "title": "Solicitar calendario de la última semana"}
    base.update(metadata)
    return SimpleNamespace(
        id="t1", agent_id="den", source_agent_id="sal", title="Solicitar calendario", metadata_json=base
    )


class TestDelegationNotice(unittest.IsolatedAsyncioTestCase):
    def _owner(self):
        return SimpleNamespace(id="u1", is_active=True, teams_id=None)

    async def test_owner_is_told_who_asked_and_the_agents_answer(self):
        sesion = _session(self._owner(), "Agente_SalaNorte", [_key()])
        n = await enqueue_delegation_notice(sesion, _task(), success=True, summary="Aquí va tu calendario…")
        self.assertEqual(n, 1)
        aviso = sesion.add.call_args.args[0]
        self.assertIn("Agente_SalaNorte", aviso.body)
        self.assertIn("Solicitar calendario de la última semana", aviso.body)
        self.assertIn("Respuesta de tu agente", aviso.body)
        self.assertEqual(aviso.task_id, "t1")
        self.assertIsNone(aviso.schedule_id)

    async def test_identity_failure_adds_a_helpful_note(self):
        sesion = _session(self._owner(), "Agente_SalaNorte", [_key()])
        n = await enqueue_delegation_notice(
            sesion, _task(), success=False, summary="No se pudo determinar el usuario de esta tarea."
        )
        self.assertEqual(n, 1)
        self.assertIn("cuenta M365", sesion.add.call_args.args[0].body)

    async def test_non_delegated_tasks_get_nothing(self):
        sesion = _session(self._owner(), "x", [_key()])
        for metadata in (
            {"delegated": False},
            {"delegated": True, "scheduled": True},
            {"delegated": True, "background_job": True},
        ):
            self.assertEqual(await enqueue_delegation_notice(sesion, _task(**metadata), success=True, summary="x"), 0)
        sesion.add.assert_not_called()

    async def test_without_a_bound_owner_nothing_is_queued(self):
        sesion = _session(self._owner(), "x", [])
        self.assertEqual(await enqueue_delegation_notice(sesion, _task(), success=True, summary="x"), 0)


if __name__ == "__main__":
    unittest.main()
