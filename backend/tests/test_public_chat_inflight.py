import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, PublicChatService, SimpleRateLimiter


def _running_task(prompt="genera el informe anual de ventas", minutes=4, **metadata):
    inicio = datetime.now(UTC) - timedelta(minutes=minutes)
    base = {"conversation": True, "source": "public_chat", "public_session_id": "s1", "thread_user_id": "u1"}
    base.update(metadata)
    return SimpleNamespace(
        id="t-run",
        status="running",
        started_at=inicio,
        queued_at=inicio,
        prompt=prompt,
        metadata_json=base,
    )


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _db(items):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(items))
    return db


class TestInflightFollowup(unittest.IsolatedAsyncioTestCase):
    def _service(self, tasks):
        service = PublicChatService.__new__(PublicChatService)
        service.rate_limiter = SimpleRateLimiter(max_requests=100)
        service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
        service.supervisor = SimpleNamespace(submit_task=AsyncMock())
        session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None, sender_id=None)
        service.validate_session = AsyncMock(return_value=session)
        service._check_monthly_quota = AsyncMock()
        service._resolve_identity = AsyncMock(return_value=("u1", None))
        db = MagicMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id="k1", bound_user_id=None, requests_per_month=0, tokens_per_month=0)
        )
        db.execute = AsyncMock(return_value=_Result(tasks))
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        return service, db

    async def test_followup_while_own_task_runs_gets_immediate_reply_without_new_run(self):
        service, db = self._service([_running_task()])
        task_id, reply = await service.send_message("s1", "tok", "mejor hazlo en excel", db)
        self.assertIsNone(task_id)
        service.supervisor.submit_task.assert_not_called()
        self.assertIn("solicitud anterior", reply)
        self.assertIn("no alcancé a procesar", reply.lower())
        self.assertIn("reenví", reply.lower())
        self.assertIn("cancela", reply)
        self.assertEqual(db.add.call_count, 2)
        for call in db.add.call_args_list:
            self.assertFalse(hasattr(call.args[0], "metadata_json"))
        db.commit.assert_awaited()

    async def test_no_running_task_does_not_short_circuit(self):
        failed = _running_task()
        failed.status = "failed"
        service = PublicChatService.__new__(PublicChatService)
        session = SimpleNamespace(agent_id="a1")
        self.assertIsNone(await service._running_interactive_task(_db([failed]), session, "s1", "u1"))

    async def test_background_job_does_not_block_interactive_followup(self):
        bg = _running_task(background_job=True)
        service = PublicChatService.__new__(PublicChatService)
        session = SimpleNamespace(agent_id="a1")
        self.assertIsNone(await service._running_interactive_task(_db([bg]), session, "s1", "u1"))

    async def test_scheduled_task_does_not_block_interactive_followup(self):
        sched = _running_task(scheduled=True)
        service = PublicChatService.__new__(PublicChatService)
        session = SimpleNamespace(agent_id="a1")
        self.assertIsNone(await service._running_interactive_task(_db([sched]), session, "s1", "u1"))

    async def test_only_the_users_own_task_triggers_the_reply(self):
        other = _running_task(public_session_id="otra", thread_user_id="u2")
        service = PublicChatService.__new__(PublicChatService)
        session = SimpleNamespace(agent_id="a1")
        self.assertIsNone(await service._running_interactive_task(_db([other]), session, "s1", "u1"))

    async def test_anonymous_only_matches_its_own_session(self):
        other_anon = _running_task(public_session_id="otra", thread_user_id=PUBLIC_CHAT_USER_ID)
        service = PublicChatService.__new__(PublicChatService)
        session = SimpleNamespace(agent_id="a1")
        self.assertIsNone(
            await service._running_interactive_task(_db([other_anon]), session, "s1", PUBLIC_CHAT_USER_ID)
        )

    def test_reply_reports_elapsed_time_and_the_prior_prompt(self):
        reply = PublicChatService._inflight_followup_reply(_running_task(prompt="dame el reporte", minutes=3))
        self.assertIn("3 min", reply)
        self.assertIn("«dame el reporte»", reply)


if __name__ == "__main__":
    unittest.main()


class TestLongRequestReplyIsInline(unittest.IsolatedAsyncioTestCase):
    async def test_background_notice_is_returned_inline_so_the_stream_closes(self):
        from hermeshq.services.public_chat_service import AVISO_PEDIDO_LARGO

        service, db = TestInflightFollowup()._service([])
        db.get = AsyncMock(
            return_value=SimpleNamespace(
                id="a1", status="running", bound_user_id=None, requests_per_month=0, tokens_per_month=0
            )
        )
        task_id, reply = await service.send_message(
            "s1",
            "tok",
            "Haz un informe interactivo por niveles sobre migrar un servidor de correo: costos y riesgos.",
            db,
        )
        self.assertIsNone(task_id)
        self.assertEqual(reply, AVISO_PEDIDO_LARGO)
        service.supervisor.submit_task.assert_awaited()
