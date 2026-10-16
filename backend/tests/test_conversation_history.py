import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.models.base import utcnow
from hermeshq.services.agent_supervisor import HISTORY_USER_MARKER, AgentSupervisor, history_assistant_text


class TestHistoryAssistantText(unittest.TestCase):
    def test_long_answer_is_trimmed(self):
        text = history_assistant_text("x" * 9000, 4000)
        self.assertLess(len(text), 4100)
        self.assertIn("recortada", text)

    def test_short_answer_is_untouched(self):
        self.assertEqual(history_assistant_text("hola", 4000), "hola")

    def test_zero_limit_disables_trimming(self):
        self.assertEqual(len(history_assistant_text("x" * 9000, 0)), 9000)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


class _Session:
    def __init__(self, items):
        self.items = items

    async def execute(self, _query):
        return _Result(self.items)


def _turn(idx, hours_ago, status="completed", response="ok"):
    return SimpleNamespace(
        id=f"t{idx}",
        prompt=f"pregunta {idx}",
        status=status,
        response=response,
        error_message="timeout" if status == "failed" else None,
        messages_json=[],
        queued_at=utcnow() - timedelta(hours=hours_ago),
        metadata_json={"conversation": True},
    )


class TestBuildConversationHistory(unittest.IsolatedAsyncioTestCase):
    async def _history(self, items, max_age=3):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        current = SimpleNamespace(id="now", metadata_json={"conversation": True}, agent_id="a")
        settings = SimpleNamespace(conversation_history_max_age_hours=max_age, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            return await supervisor._build_conversation_history(_Session(list(reversed(items))), current)

    async def test_old_turns_are_dropped(self):
        history = await self._history([_turn(1, 10), _turn(2, 5), _turn(3, 1)])
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], [HISTORY_USER_MARKER + "pregunta 3"])

    async def test_max_age_zero_keeps_everything(self):
        history = await self._history([_turn(1, 100), _turn(2, 1)], max_age=0)
        self.assertEqual(len([m for m in history if m["role"] == "user"]), 2)

    async def test_failed_turn_is_left_out_of_the_history(self):
        history = await self._history([_turn(1, 1, status="failed", response="texto a medias")])
        self.assertEqual(history, [])

    async def test_every_prior_user_message_is_marked_as_already_answered(self):
        history = await self._history([_turn(1, 1), _turn(2, 1)])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(len(users), 2)
        self.assertTrue(all(u.startswith("[Mensaje anterior, ya respondido") for u in users))
