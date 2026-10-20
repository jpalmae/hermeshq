import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.services.public_chat_service import (
    PUBLIC_CHAT_USER_ID,
    BindingError,
    PublicChatService,
    _binding_problem,
)


def _key(**overrides):
    base = {
        "id": "key-1",
        "agent_id": "agent-1",
        "bound_user_id": None,
        "trust_sender_id": False,
        "is_active": True,
        "widget_title": None,
        "widget_theme": "auto",
        "widget_accent": "#6366f1",
        "widget_position": "right",
        "requests_per_month": 1000,
        "tokens_per_month": 1000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(user=None, assigned=True):
    db = AsyncMock()
    db.get.return_value = user
    result = MagicMock()
    result.first.return_value = ("x",) if assigned else None
    db.execute.return_value = result
    return db


class TestBindingProblem(unittest.IsolatedAsyncioTestCase):
    async def test_valid(self):
        db = _db(SimpleNamespace(is_active=True))
        self.assertIsNone(await _binding_problem(db, "agent-1", "u1"))

    async def test_missing_user(self):
        self.assertEqual(await _binding_problem(_db(None), "agent-1", "u1"), "Bound user not found")

    async def test_inactive_user(self):
        db = _db(SimpleNamespace(is_active=False))
        self.assertEqual(await _binding_problem(db, "agent-1", "u1"), "Bound user is inactive")

    async def test_not_assigned(self):
        db = _db(SimpleNamespace(is_active=True), assigned=False)
        self.assertEqual(await _binding_problem(db, "agent-1", "u1"), "Bound user is not assigned to this agent")


class TestKeyManagement(unittest.IsolatedAsyncioTestCase):
    def _service(self):
        return PublicChatService.__new__(PublicChatService)

    async def test_bind_rejects_widget_config(self):
        db = _db(SimpleNamespace(is_active=True))
        with self.assertRaises(BindingError):
            await self._service()._check_binding(_key(bound_user_id="u1", widget_title="Hola"), db)

    async def test_bind_rejects_invalid_user(self):
        with self.assertRaises(BindingError):
            await self._service()._check_binding(_key(bound_user_id="u1"), _db(None))

    async def test_bind_ok(self):
        db = _db(SimpleNamespace(is_active=True))
        await self._service()._check_binding(_key(bound_user_id="u1"), db)

    async def test_update_rolls_back_on_invalid_binding(self):
        api_key = _key()
        db = _db(None)
        db.get.side_effect = lambda model, _id: api_key if model.__name__ == "PublicChatApiKey" else None
        payload = SimpleNamespace(safe_update_dict=lambda: {"bound_user_id": "u1"})
        with self.assertRaises(BindingError):
            await self._service().update_api_key("key-1", payload, db)
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()


class TestSendMessageIdentity(unittest.IsolatedAsyncioTestCase):
    async def _send(self, api_key, user, assigned=True, sender_id=None, resolved=None):
        service = PublicChatService.__new__(PublicChatService)
        service.rate_limiter = MagicMock(check=MagicMock(return_value=True))
        service.ip_rate_limiter = MagicMock(check=MagicMock(return_value=True))
        service.supervisor = MagicMock(submit_task=AsyncMock())
        session = SimpleNamespace(api_key_id=api_key.id, agent_id="agent-1", last_activity=None, sender_id=sender_id)
        service.validate_session = AsyncMock(return_value=session)
        service._check_monthly_quota = AsyncMock()

        agent = SimpleNamespace(id="agent-1", status="running")
        db = AsyncMock()
        db.add = MagicMock()

        async def _get(model, _id):
            name = model.__name__
            if name == "PublicChatApiKey":
                return api_key
            if name == "Agent":
                return agent
            if name == "User":
                return user
            return None

        db.get.side_effect = _get
        result = MagicMock()
        result.first.return_value = ("x",) if assigned else None
        db.execute.return_value = result
        with patch(
            "hermeshq.services.public_chat_service.resolve_channel_user", AsyncMock(return_value=resolved)
        ) as resolver:
            await service.send_message("s1", "tok", "hola", db)
        self.resolver = resolver
        tasks = [c.args[0] for c in db.add.call_args_list if hasattr(c.args[0], "metadata_json")]
        return tasks[0]

    async def test_unbound_key_is_anonymous(self):
        task = await self._send(_key(), None)
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)
        self.assertIsNone(task.created_by_user_id)

    async def test_bound_key_uses_user(self):
        task = await self._send(_key(bound_user_id="u1"), SimpleNamespace(is_active=True))
        self.assertEqual(task.metadata_json["thread_user_id"], "u1")
        self.assertEqual(task.metadata_json["public_chat_bound_user_id"], "u1")
        self.assertIsNone(task.created_by_user_id)

    async def test_inactive_user_degrades(self):
        task = await self._send(_key(bound_user_id="u1"), SimpleNamespace(is_active=False))
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)
        self.assertIn("public_chat_binding_denied", task.metadata_json)

    async def test_unassigned_user_degrades(self):
        task = await self._send(_key(bound_user_id="u1"), SimpleNamespace(is_active=True), assigned=False)
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)

    async def test_trusted_sender_resolves_user(self):
        task = await self._send(
            _key(trust_sender_id=True),
            SimpleNamespace(is_active=True),
            sender_id="29:abc",
            resolved=SimpleNamespace(id="u2"),
        )
        self.resolver.assert_awaited_once()
        self.assertEqual(self.resolver.await_args.args[1:], ("microsoft_teams", "29:abc"))
        self.assertEqual(task.metadata_json["thread_user_id"], "u2")
        self.assertEqual(task.metadata_json["public_chat_sender_id"], "29:abc")

    async def test_trusted_sender_unknown_degrades(self):
        task = await self._send(
            _key(trust_sender_id=True), SimpleNamespace(is_active=True), sender_id="29:zzz", resolved=None
        )
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)
        self.assertEqual(task.metadata_json["public_chat_binding_denied"], "Sender is not linked to any user")

    async def test_trusted_sender_user_not_assigned_degrades(self):
        task = await self._send(
            _key(trust_sender_id=True),
            SimpleNamespace(is_active=True),
            assigned=False,
            sender_id="29:abc",
            resolved=SimpleNamespace(id="u2"),
        )
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)

    async def test_untrusted_key_ignores_sender_id(self):
        task = await self._send(
            _key(), SimpleNamespace(is_active=True), sender_id="29:abc", resolved=SimpleNamespace(id="u2")
        )
        self.resolver.assert_not_awaited()
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)

    async def test_trusted_without_sender_is_anonymous(self):
        task = await self._send(_key(trust_sender_id=True), SimpleNamespace(is_active=True))
        self.resolver.assert_not_awaited()
        self.assertEqual(task.metadata_json["thread_user_id"], PUBLIC_CHAT_USER_ID)


class TestSessionSender(unittest.IsolatedAsyncioTestCase):
    async def _create(self, key, sender_id):
        service = PublicChatService.__new__(PublicChatService)
        service.session_create_limiter = MagicMock(check=MagicMock(return_value=True))
        db = AsyncMock()
        db.add = MagicMock()
        count = MagicMock()
        count.scalar.return_value = 0
        db.execute.return_value = count
        db.get.return_value = SimpleNamespace(friendly_name="Op", name="op")
        await service.create_session(key, db, sender_id=sender_id)
        return db.add.call_args.args[0]

    async def test_trusted_key_stores_sender(self):
        session = await self._create(_key(trust_sender_id=True), " 29:abc ")
        self.assertEqual(session.sender_id, "29:abc")

    async def test_untrusted_key_drops_sender(self):
        session = await self._create(_key(), "29:abc")
        self.assertIsNone(session.sender_id)

    async def test_bound_and_trusted_rejected(self):
        service = PublicChatService.__new__(PublicChatService)
        db = _db(SimpleNamespace(is_active=True))
        with self.assertRaises(BindingError):
            await service._check_binding(_key(bound_user_id="u1", trust_sender_id=True), db)

    async def test_trusted_with_widget_rejected(self):
        service = PublicChatService.__new__(PublicChatService)
        with self.assertRaises(BindingError):
            await service._check_binding(_key(trust_sender_id=True, widget_title="x"), _db(None))
