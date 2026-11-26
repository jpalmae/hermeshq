import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, PublicChatService, SimpleRateLimiter


def _task(task_id, session_id="s1", user_id="u1", source="public_chat", status="running"):
    return SimpleNamespace(
        id=task_id,
        status=status,
        completed_at=None,
        board_column=None,
        metadata_json={"source": source, "public_session_id": session_id, "thread_user_id": user_id},
    )


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _service(tasks, active=("t1",)):
    service = PublicChatService.__new__(PublicChatService)
    service.rate_limiter = SimpleRateLimiter(max_requests=100)
    service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
    service.supervisor = SimpleNamespace(active_tasks={tid: object() for tid in active}, cancel_task=AsyncMock())
    session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None)
    service.validate_session = AsyncMock(return_value=session)
    service._resolve_identity = AsyncMock(return_value=("u1", None))
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id="k1"))
    db.execute = AsyncMock(return_value=_Result(tasks))
    db.commit = AsyncMock()
    db.add = MagicMock()
    return service, db


class TestHandleCancelRequest(unittest.IsolatedAsyncioTestCase):
    async def test_not_a_cancel_returns_none_and_touches_nothing(self):
        service, db = _service([_task("t1")])
        self.assertIsNone(await service.handle_cancel_request("s1", "tok", "dame el html", db))
        db.execute.assert_not_called()
        service.supervisor.cancel_task.assert_not_called()

    async def test_cancel_stops_the_running_task_of_this_chat(self):
        service, db = _service([_task("t1")])
        reply = await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")
        self.assertIn("cancelé", reply)
        self.assertEqual(db.add.call_count, 2)

    async def test_other_users_task_on_the_same_agent_is_left_alone(self):
        service, db = _service([_task("t1", session_id="otra", user_id="u2")])
        reply = await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()
        self.assertIn("No tengo ninguna tarea", reply)

    async def test_task_of_an_older_session_of_the_same_user_is_cancelled(self):
        service, db = _service([_task("t1", session_id="vieja", user_id="u1")])
        await service.handle_cancel_request("s1", "tok", "stop", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")

    async def test_anonymous_user_only_cancels_its_own_session(self):
        service, db = _service([_task("t1", session_id="otra", user_id=PUBLIC_CHAT_USER_ID)])
        service._resolve_identity = AsyncMock(return_value=(PUBLIC_CHAT_USER_ID, None))
        await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_non_chat_tasks_are_never_cancelled(self):
        service, db = _service([_task("t1", source="scheduled")])
        await service.handle_cancel_request("s1", "tok", "para", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_queued_task_without_runner_is_marked_cancelled_directly(self):
        queued = _task("t2", status="queued")
        service, db = _service([queued], active=())
        await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()
        self.assertEqual(queued.status, "cancelled")
        self.assertIsNotNone(queued.completed_at)

    async def test_two_tasks_give_plural_reply(self):
        service, db = _service([_task("t1"), _task("t3")], active=("t1", "t3"))
        reply = await service.handle_cancel_request("s1", "tok", "para", db)
        self.assertIn("2 tareas", reply)
        self.assertEqual(service.supervisor.cancel_task.await_count, 2)


def _corrida(task_id, user_id="u1", manual=True, desde_chat=True):
    return SimpleNamespace(
        id=task_id,
        status="running",
        completed_at=None,
        board_column=None,
        metadata_json={
            "scheduled": True,
            "scheduled_task_id": "cron-1",
            "thread_user_id": user_id,
            **({"manual_trigger": True} if manual else {}),
            **({"solicitado_desde_chat": True} if desde_chat else {}),
        },
    )


class TestCancelaCorridaPedidaDesdeElChat(unittest.IsolatedAsyncioTestCase):
    async def test_cancela_detiene_la_corrida_manual_que_pidio_este_usuario(self):
        service, db = _service([_corrida("t1")])
        reply = await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")
        self.assertIn("cancelé la tarea en curso", reply)

    async def test_la_corrida_automatica_del_cron_no_se_toca(self):
        service, db = _service([_corrida("t1", manual=False)])
        reply = await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()
        self.assertIn("No tengo ninguna tarea", reply)

    async def test_corrida_manual_lanzada_desde_el_panel_no_se_toca(self):
        service, db = _service([_corrida("t1", desde_chat=False)])
        await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_corrida_de_otro_usuario_no_se_toca(self):
        service, db = _service([_corrida("t1", user_id="u2")])
        await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_usuario_anonimo_no_cancela_corridas(self):
        service, db = _service([_corrida("t1", user_id=PUBLIC_CHAT_USER_ID)])
        service._resolve_identity = AsyncMock(return_value=(PUBLIC_CHAT_USER_ID, None))
        await service.handle_cancel_request("s1", "tok", "cancela", db)
        service.supervisor.cancel_task.assert_not_called()

    async def test_reset_no_cancela_la_corrida_pero_si_la_conversacion(self):
        service, db = _service([_corrida("t1"), _task("t2")], active=("t1", "t2"))
        reply = await service.handle_cancel_request("s1", "tok", "/reset", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t2")
        self.assertIn("Cancelé 1 tarea", reply)


class TestCancelaUnObjetoSinNadaEnCurso(unittest.IsolatedAsyncioTestCase):
    async def test_caso_real_ahora_cancelala_sin_tareas_va_al_agente(self):
        service, db = _service([], active=())
        self.assertIsNone(await service.handle_cancel_request("s1", "tok", "Ahora cancélala.", db))
        service.supervisor.cancel_task.assert_not_called()
        db.add.assert_not_called()

    async def test_cancelala_con_una_tarea_en_curso_la_detiene(self):
        service, db = _service([_task("t1")])
        reply = await service.handle_cancel_request("s1", "tok", "cancélala", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t1")
        self.assertIn("cancelé", reply)

    async def test_cancela_a_secas_sin_tareas_sigue_respondiendo(self):
        service, db = _service([], active=())
        reply = await service.handle_cancel_request("s1", "tok", "cancela", db)
        self.assertIn("No tengo ninguna tarea", reply)


class TestApuntaAUnObjeto(unittest.TestCase):
    def test_formas(self):
        from hermeshq.services.cancel_intent import apunta_a_un_objeto

        for texto in ("Ahora cancélala.", "cancélalo", "olvídala", "cancela la reunión", "borra ese cron"):
            self.assertTrue(apunta_a_un_objeto(texto), texto)
        for texto in ("cancela", "para", "stop", "basta ya", "detente"):
            self.assertFalse(apunta_a_un_objeto(texto), texto)
