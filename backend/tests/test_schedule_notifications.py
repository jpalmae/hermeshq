import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from hermeshq.routers import public_chat
from hermeshq.services.public_chat_service import SimpleRateLimiter
from hermeshq.services.schedule_notifications import (
    MAX_BODY_CHARS,
    _clip,
    _failure_body_detail,
    enqueue_schedule_notification,
    friendly_error_text,
)


def _task(**metadata):
    base = {"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": "u1"}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", created_by_user_id="u1", metadata_json=base)


def _schedule(**overrides):
    base = {"id": "s1", "agent_id": "a1", "name": "Informe", "notify_public_chat": True, "owner_user_id": "u1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(**overrides):
    base = {"id": "u1", "is_active": True, "teams_id": "29:abc"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _key(**overrides):
    base = {
        "id": "k1",
        "trust_sender_id": False,
        "bound_user_id": "u1",
        "last_notification_poll_at": datetime.now(UTC) - timedelta(seconds=30),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(schedule, user, keys):
    session = MagicMock()
    session.get = AsyncMock(side_effect=lambda model, key: schedule if model.__name__ == "ScheduledTask" else user)
    result = MagicMock()
    result.scalars.return_value.all.return_value = keys
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    return session


class EnqueueNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_one_notification_per_matching_key(self):
        session = _session(_schedule(), _user(), [_key()])
        count = await enqueue_schedule_notification(session, _task(), success=True, summary="todo ok")
        self.assertEqual(count, 1)
        notification = session.add.call_args.args[0]
        self.assertEqual(notification.api_key_id, "k1")
        self.assertEqual(notification.recipient_user_id, "u1")
        self.assertEqual(notification.title, "Informe")
        self.assertEqual(notification.body, "todo ok")
        self.assertEqual(notification.task_id, "t1")
        self.assertIsNone(notification.sender_id)

    async def test_sender_id_set_for_trusted_sender_keys(self):
        session = _session(_schedule(), _user(), [_key(trust_sender_id=True, bound_user_id=None)])
        await enqueue_schedule_notification(session, _task(), success=True, summary="x")
        self.assertEqual(session.add.call_args.args[0].sender_id, "29:abc")

    async def test_cron_is_queued_even_before_the_bridge_has_polled(self):
        session = _session(_schedule(), _user(), [_key(last_notification_poll_at=None)])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 1)
        self.assertEqual(session.add.call_args.args[0].api_key_id, "k1")

    async def test_cron_is_queued_when_the_bridge_went_quiet(self):
        stale = datetime.now(UTC) - timedelta(hours=13)
        session = _session(_schedule(), _user(), [_key(last_notification_poll_at=stale)])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 1)
        self.assertEqual(session.add.call_args.args[0].api_key_id, "k1")

    async def test_only_live_keys_get_the_notification(self):
        live = _key(id="live")
        quiet = _key(id="quiet", last_notification_poll_at=None)
        session = _session(_schedule(), _user(), [live, quiet])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 1)
        self.assertEqual(session.add.call_args.args[0].api_key_id, "live")

    async def test_failure_body_is_prefixed(self):
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(session, _task(), success=False, summary="boom")
        self.assertTrue(session.add.call_args.args[0].body.startswith("La tarea programada falló:"))

    async def test_scheduled_failure_translates_runtime_error_to_spanish(self):
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(
            session, _task(), success=False, summary="Hermes runtime exceeded the task timeout of 900s"
        )
        body = session.add.call_args.args[0].body
        self.assertTrue(body.startswith("La tarea programada falló:"))
        self.assertIn("tardó demasiado", body)
        self.assertNotIn("exceeded the task timeout", body)

    async def test_background_failure_body_is_spanish(self):
        session = _session(_schedule(), _user(), [_key()])
        task = SimpleNamespace(
            id="t1",
            agent_id="a1",
            metadata_json={"background_job": True, "thread_user_id": "u1", "background_title": "Informe"},
        )
        await enqueue_schedule_notification(
            session, task, success=False, summary="Hermes runtime exceeded the task timeout of 1800s"
        )
        body = session.add.call_args.args[0].body
        self.assertIn("segundo plano", body)
        self.assertIn("tardó demasiado", body)
        self.assertNotIn("exceeded the task timeout", body)

    async def test_scheduled_failure_keeps_curated_spanish_summary(self):
        session = _session(_schedule(), _user(), [_key()])
        curado = "el servidor se reinició mientras lo trabajaba; vuelve a pedirlo"
        await enqueue_schedule_notification(session, _task(), success=False, summary=curado)
        body = session.add.call_args.args[0].body
        self.assertIn("vuelve a pedirlo", body)
        self.assertNotIn("Hubo un problema", body)
        self.assertNotIn("acótala o pídela por partes", body)

    async def test_background_failure_keeps_curated_spanish_summary(self):
        session = _session(_schedule(), _user(), [_key()])
        task = SimpleNamespace(
            id="t1",
            agent_id="a1",
            metadata_json={"background_job": True, "thread_user_id": "u1", "background_title": "Informe"},
        )
        curado = "el servidor se reinició mientras lo trabajaba; vuelve a pedirlo"
        await enqueue_schedule_notification(session, task, success=False, summary=curado)
        body = session.add.call_args.args[0].body
        self.assertIn("segundo plano", body)
        self.assertIn("vuelve a pedirlo", body)
        self.assertNotIn("Hubo un problema", body)

    async def test_skips_when_schedule_does_not_notify(self):
        session = _session(_schedule(notify_public_chat=False), _user(), [_key()])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 0)
        session.add.assert_not_called()

    async def test_skips_manual_and_unscheduled_runs(self):
        session = _session(_schedule(), _user(), [_key()])
        self.assertEqual(
            await enqueue_schedule_notification(session, _task(manual_trigger=True), success=True, summary="x"), 0
        )
        plain = SimpleNamespace(id="t2", metadata_json={})
        self.assertEqual(await enqueue_schedule_notification(session, plain, success=True, summary="x"), 0)
        session.add.assert_not_called()

    async def test_manual_run_requested_from_chat_is_delivered(self):
        session = _session(_schedule(), _user(), [_key()])
        tarea = _task(manual_trigger=True, solicitado_desde_chat=True)
        self.assertEqual(await enqueue_schedule_notification(session, tarea, success=True, summary="resumen"), 1)
        aviso = session.add.call_args.args[0]
        self.assertEqual(aviso.title, "Informe (ejecución manual)")
        self.assertEqual(aviso.body, "resumen")

    async def test_failed_manual_run_requested_from_chat_says_it_failed(self):
        session = _session(_schedule(), _user(), [_key()])
        tarea = _task(manual_trigger=True, solicitado_desde_chat=True)
        await enqueue_schedule_notification(
            session, tarea, success=False, summary="Hermes runtime exceeded the task timeout of 900s"
        )
        self.assertTrue(session.add.call_args.args[0].body.startswith("La tarea programada falló"))

    async def test_admitted_failure_never_gets_a_check_mark(self):
        texto = "No pude generar el enlace de solo lectura porque esta acción se está intentando desde segundo plano."
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(
            session,
            _task(scheduled=False, background_job=True, background_title="Compárteme"),
            success=True,
            summary=texto,
        )
        cuerpo = session.add.call_args.args[0].body
        self.assertNotIn("✅", cuerpo)
        self.assertTrue(cuerpo.startswith("⚠️"))
        self.assertIn("sin hacer lo pedido", cuerpo)
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(session, _task(), success=True, summary=texto)
        self.assertTrue(session.add.call_args.args[0].body.startswith("⚠️ La tarea programada terminó sin hacer"))

    async def test_success_that_mentions_a_failure_later_keeps_the_check_mark(self):
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(
            session,
            _task(scheduled=False, background_job=True, background_title="Excel"),
            success=True,
            summary="Listo, subí el Excel. Nota: no pude leer la hoja oculta, pero no era necesaria.",
        )
        self.assertIn("✅", session.add.call_args.args[0].body)

    async def test_skips_without_owner_inactive_user_or_keys(self):
        for schedule, user, keys in (
            (_schedule(owner_user_id=None), _user(), [_key()]),
            (_schedule(), _user(is_active=False), [_key()]),
            (_schedule(), _user(), []),
        ):
            session = _session(schedule, user, keys)
            self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 0)
            session.add.assert_not_called()

    async def test_never_raises_into_the_task(self):
        session = MagicMock()
        session.get = AsyncMock(side_effect=RuntimeError("db down"))
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 0)

    async def test_long_results_are_clipped(self):
        session = _session(_schedule(), _user(), [_key()])
        await enqueue_schedule_notification(session, _task(), success=True, summary="x" * (MAX_BODY_CHARS + 500))
        self.assertLessEqual(len(session.add.call_args.args[0].body), MAX_BODY_CHARS)
        self.assertIn("recortado", session.add.call_args.args[0].body)

    def test_clip_keeps_short_text(self):
        self.assertEqual(_clip("  hola  ", 100), "hola")


class NotificationEndpointTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, api_key=None, error=None):
        service = MagicMock()
        service.validate_api_key = AsyncMock(side_effect=error) if error else AsyncMock(return_value=api_key)
        service.notification_limiter = SimpleRateLimiter(max_requests=3, window_seconds=60)
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(public_chat_service=service)),
            headers={"origin": "https://bridge"},
        )

    def _notification(self, **overrides):
        base = {
            "id": "n1",
            "api_key_id": "k1",
            "title": "Informe",
            "body": "ok",
            "created_at": datetime.now(UTC),
            "sender_id": None,
            "task_id": "t1",
            "schedule_id": "s1",
            "status": "pending",
            "delivered_at": None,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    async def test_list_requires_valid_api_key(self):
        request = self._request(error=ValueError("Invalid or inactive API key"))
        with self.assertRaises(HTTPException) as ctx:
            await public_chat.list_notifications(request, 20, "bad", MagicMock())
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_list_returns_pending_notifications_of_the_key(self):
        request = self._request(api_key=SimpleNamespace(id="k1"))
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [self._notification()]
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        api_key = request.app.state.public_chat_service.validate_api_key.return_value
        items = await public_chat.list_notifications(request, 20, "key", db)
        self.assertIsNotNone(api_key.last_notification_poll_at)
        self.assertEqual([item.id for item in items], ["n1"])
        self.assertEqual(items[0].body, "ok")
        compiled = next(
            str(c.args[0].compile(dialect=postgresql.dialect()))
            for c in db.execute.await_args_list
            if str(c.args[0]).startswith("SELECT public_chat_notifications")
        )
        self.assertIn("api_key_id", compiled)
        self.assertIn("status", compiled)
        self.assertIn("created_at >", compiled)

    def test_delivery_window_is_shorter_than_retention(self):
        from hermeshq.services.schedule_notifications import NOTIFICATION_DELIVERY_WINDOW, NOTIFICATION_RETENTION

        self.assertEqual(NOTIFICATION_DELIVERY_WINDOW, timedelta(hours=12))
        self.assertLess(NOTIFICATION_DELIVERY_WINDOW, NOTIFICATION_RETENTION)

    async def test_ack_marks_delivered_and_is_idempotent(self):
        request = self._request(api_key=SimpleNamespace(id="k1"))
        notification = self._notification()
        db = MagicMock()
        db.get = AsyncMock(return_value=notification)
        db.commit = AsyncMock()
        first = await public_chat.acknowledge_notification("n1", request, "key", db)
        self.assertTrue(first["acknowledged"])
        self.assertEqual(notification.status, "delivered")
        self.assertIsNotNone(notification.delivered_at)
        delivered_at = notification.delivered_at
        await public_chat.acknowledge_notification("n1", request, "key", db)
        self.assertEqual(notification.delivered_at, delivered_at)
        self.assertEqual(db.commit.await_count, 1)

    async def test_ack_of_another_keys_notification_is_404(self):
        request = self._request(api_key=SimpleNamespace(id="k2"))
        db = MagicMock()
        db.get = AsyncMock(return_value=self._notification(api_key_id="k1"))
        with self.assertRaises(HTTPException) as ctx:
            await public_chat.acknowledge_notification("n1", request, "key", db)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_polling_is_rate_limited(self):
        request = self._request(api_key=SimpleNamespace(id="k1"))
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        for _ in range(3):
            await public_chat.list_notifications(request, 20, "key", db)
        with self.assertRaises(HTTPException) as ctx:
            await public_chat.list_notifications(request, 20, "key", db)
        self.assertEqual(ctx.exception.status_code, 429)


class FriendlyErrorTextTests(unittest.TestCase):
    def test_timeout_maps_to_spanish(self):
        msg = friendly_error_text("Hermes runtime exceeded the task timeout of 900s")
        self.assertIn("tardó demasiado", msg)
        self.assertNotIn("timeout", msg.lower())

    def test_no_output_maps_to_spanish(self):
        self.assertIn("tardó demasiado", friendly_error_text("Hermes runtime produced no output for 600s"))

    def test_invalid_tool_call_maps_to_spanish(self):
        msg = friendly_error_text("Model generated invalid tool call")
        self.assertIn("llamada inválida", msg)
        self.assertIn("por partes", msg)

    def test_unknown_error_is_generic_spanish_with_next_step(self):
        msg = friendly_error_text("Traceback: KeyError 'x'")
        self.assertIn("Hubo un problema", msg)
        self.assertNotIn("Traceback", msg)
        self.assertNotIn("KeyError", msg)

    def test_none_and_empty_are_handled(self):
        self.assertIn("Hubo un problema", friendly_error_text(None))
        self.assertIn("Hubo un problema", friendly_error_text(""))

    def test_sse_and_notification_paths_share_the_spanish_text(self):
        from hermeshq.routers.public_chat import _friendly_task_error

        raw = "Hermes runtime exceeded the task timeout of 900s"
        self.assertEqual(_friendly_task_error({"error": raw}), friendly_error_text(raw))

    def test_failure_body_detail_translates_runtime_error(self):
        self.assertEqual(
            _failure_body_detail("Hermes runtime exceeded the task timeout of 900s"),
            friendly_error_text("Hermes runtime exceeded the task timeout of 900s"),
        )

    def test_failure_body_detail_preserves_curated_summary(self):
        curado = "el servidor se reinició mientras lo trabajaba; vuelve a pedirlo"
        self.assertEqual(_failure_body_detail(curado), curado)

    def test_failure_body_detail_handles_empty(self):
        self.assertEqual(_failure_body_detail(None), "error desconocido")
        self.assertEqual(_failure_body_detail(""), "error desconocido")

    def test_raw_typeerror_maps_to_spanish(self):
        raw = "sequence item 0: expected str instance, NoneType found"
        msg = friendly_error_text(raw)
        self.assertIn("error interno", msg)
        self.assertNotIn("sequence item", msg)
        self.assertNotIn("NoneType", msg)

    def test_failure_body_detail_translates_raw_typeerror(self):
        raw = "sequence item 0: expected str instance, NoneType found"
        detalle = _failure_body_detail(raw)
        self.assertEqual(detalle, friendly_error_text(raw))
        self.assertNotIn("sequence item", detalle)


if __name__ == "__main__":
    unittest.main()
