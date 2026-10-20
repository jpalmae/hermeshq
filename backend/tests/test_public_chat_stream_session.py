import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.routers import public_chat
from hermeshq.schemas.public_chat import SendMessageRequest


def _request(service):
    app = SimpleNamespace(state=SimpleNamespace(public_chat_service=service, event_broker=MagicMock()))
    return SimpleNamespace(app=app, headers={}, client=SimpleNamespace(host="1.2.3.4"))


class TestSendMessageReleasesSession(unittest.IsolatedAsyncioTestCase):
    async def test_db_session_is_closed_before_streaming(self):
        service = MagicMock()
        service.handle_cancel_request = AsyncMock(return_value=None)
        service.send_message = AsyncMock(return_value=("task-1", "agent-1"))
        db = AsyncMock()
        with patch.object(public_chat, "_get_service", return_value=service):
            response = await public_chat.send_message(
                "s1", SendMessageRequest(content="hola"), _request(service), "tok", db
            )
        db.close.assert_awaited_once()
        self.assertEqual(response.media_type, "text/event-stream")

    async def test_db_session_not_closed_early_on_error(self):
        service = MagicMock()
        service.handle_cancel_request = AsyncMock(return_value=None)
        service.send_message = AsyncMock(side_effect=ValueError("Rate limit exceeded"))
        db = AsyncMock()
        with (
            patch.object(public_chat, "_get_service", return_value=service),
            self.assertRaises(HTTPException),
        ):
            await public_chat.send_message("s1", SendMessageRequest(content="hola"), _request(service), "tok", db)
        db.close.assert_not_awaited()


class TestCancelRouteShortCircuits(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_message_never_creates_a_task(self):
        service = MagicMock()
        service.handle_cancel_request = AsyncMock(return_value="⏹️ Listo, cancelé la tarea en curso.")
        service.send_message = AsyncMock()
        db = MagicMock()
        db.close = AsyncMock()
        response = await public_chat.send_message(
            "s1", SendMessageRequest(content="para"), _request(service), "tok", db
        )
        service.send_message.assert_not_called()
        chunks = [chunk async for chunk in response.body_iterator]
        events = [json.loads(str(chunk).removeprefix("data: ")) for chunk in chunks if str(chunk).startswith("data: ")]
        self.assertEqual(events[0]["type"], "done")
        self.assertIn("cancelé", events[0]["content"])
