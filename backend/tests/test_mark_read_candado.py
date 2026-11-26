import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load


class TestMarkReadCandado(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _mark(self, args, conocidos=None):
        if conocidos is not None:
            self.mod._remember_ids([{"id": i} for i in conocidos])

        def graph(method, path, token, payload=None, **kwargs):
            self.calls.append((method, path))
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(self.mod._mark_read_tool(args))

    def _patched(self):
        return [p for m, p in self.calls if m == "PATCH"]

    def test_marks_only_known_ids_and_reports_the_unknown(self):
        r = self._mark({"message_ids": ["KNOWN1", "FAKE1"]}, conocidos=["KNOWN1", "KNOWN2"])
        self.assertEqual(r["marcados"], ["KNOWN1"])
        self.assertEqual(r["ids_no_reconocidos"], ["FAKE1"])
        self.assertEqual(self._patched(), ["/me/messages/KNOWN1"])

    def test_rejects_when_every_id_is_unknown(self):
        r = self._mark({"message_ids": ["FAKE1", "FAKE2"]}, conocidos=["KNOWN1"])
        self.assertFalse(r["success"])
        self.assertIn("No marqué", r["error"])
        self.assertEqual(self._patched(), [])

    def test_empty_cache_falls_back_to_allowing(self):
        r = self._mark({"message_ids": ["ANY1"]})
        self.assertEqual(r["marcados"], ["ANY1"])
        self.assertNotIn("ids_no_reconocidos", r)

    def test_get_many_records_ids_so_they_can_be_marked(self):
        self.mod._remember_ids([{"id": "GOT1"}])
        r = self._mark({"message_id": "GOT1"})
        self.assertEqual(r["marcados"], ["GOT1"])


if __name__ == "__main__":
    unittest.main()
