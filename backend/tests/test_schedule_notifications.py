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
    enqueue_schedule_notification,
)


def _task(**metadata):
    base = {"scheduled": True, "scheduled_task_id": "s1"}
    base.update(metadata)
    return SimpleNamespace(id="t1", metadata_json=base)


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

    async def test_nothing_is_queued_until_the_bridge_has_polled(self):
        session = _session(_schedule(), _user(), [_key(last_notification_poll_at=None)])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 0)
        session.add.assert_not_called()

    async def test_nothing_is_queued_when_the_bridge_went_quiet(self):
        stale = datetime.now(UTC) - timedelta(hours=13)
        session = _session(_schedule(), _user(), [_key(last_notification_poll_at=stale)])
        self.assertEqual(await enqueue_schedule_notification(session, _task(), success=True, summary="x"), 0)
        session.add.assert_not_called()

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
        compiled = str(db.execute.await_args_list[-1].args[0].compile(dialect=postgresql.dialect()))
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


if __name__ == "__main__":
    unittest.main()
