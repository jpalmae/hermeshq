import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.delegation_approval import approve_delegation_identity, new_identity_code


def _db(tasks):
    db = MagicMock()
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = tasks
    db.execute = AsyncMock(return_value=resultado)
    added = []
    db.add = MagicMock(side_effect=lambda o: added.append(o))

    async def flush():
        for o in added:
            if getattr(o, "id", None) is None:
                o.id = "nuevo-id"

    db.flush = AsyncMock(side_effect=flush)
    db._added = added
    return db


def _failed_delegated(code="ABC123", owner="u1", expires=None, **meta):
    base = {
        "delegated": True,
        "delegation_owner_user_id": owner,
        "identity_approval_code": code,
        "identity_approval_expires": expires or (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "title": "Solicitar calendario",
    }
    base.update(meta)
    return SimpleNamespace(
        id="orig",
        agent_id="den",
        source_agent_id="sal",
        parent_task_id="p1",
        title="Solicitar calendario",
        prompt="dame tu calendario",
        status="failed",
        queued_at=datetime.now(UTC) - timedelta(minutes=5),
        metadata_json=base,
    )


class TestDelegationApproval(unittest.IsolatedAsyncioTestCase):
    async def test_approval_creates_a_rerun_with_identity_preserving_delivery(self):
        orig = _failed_delegated()
        db = _db([orig])
        task_id, reply = await approve_delegation_identity(db, "den", "u1", "abc123")
        self.assertEqual(task_id, "nuevo-id")
        self.assertIn("Autorizado", reply)
        nuevo = db._added[0]
        self.assertEqual(nuevo.metadata_json["thread_user_id"], "u1")
        self.assertTrue(nuevo.metadata_json["identity_granted"])
        self.assertEqual(nuevo.source_agent_id, "sal")
        self.assertEqual(nuevo.parent_task_id, "p1")
        self.assertEqual(nuevo.prompt, "dame tu calendario")
        self.assertIsNone(orig.metadata_json["identity_approval_code"])

    async def test_wrong_code_owner_or_expired_do_nothing(self):
        casos = [
            (_failed_delegated(code="ABC123"), "u1", "ZZZ999"),
            (_failed_delegated(owner="otro"), "u1", "ABC123"),
            (
                _failed_delegated(expires=(datetime.now(UTC) - timedelta(hours=1)).isoformat()),
                "u1",
                "ABC123",
            ),
            (_failed_delegated(delegated=False), "u1", "ABC123"),
        ]
        for tarea, user, code in casos:
            db = _db([tarea])
            task_id, reply = await approve_delegation_identity(db, "den", user, code)
            self.assertIsNone(task_id)
            self.assertIsNone(reply)
            db.add.assert_not_called()

    def test_identity_code_is_short_and_uppercase(self):
        code = new_identity_code()
        self.assertEqual(code, code.upper())
        self.assertTrue(4 <= len(code) <= 8)


if __name__ == "__main__":
    unittest.main()
