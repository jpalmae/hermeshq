import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_ms365_mail_get import _load


class _Base(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)


class MarkReadTests(_Base):
    def _run(self, args, graph_result):
        captured = {}

        def fake_graph(method, path, token, payload=None, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["payload"] = payload
            return graph_result

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
        ):
            return json.loads(self.plugin._mark_read_tool(args)), captured

    def test_marks_read_with_a_patch_to_the_message(self):
        out, captured = self._run({"message_id": "AAMk="}, {})
        self.assertTrue(out["success"], out)
        self.assertTrue(out["isRead"])
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["path"], "/me/messages/AAMk=")
        self.assertEqual(captured["payload"], {"isRead": True})

    def test_can_mark_as_unread(self):
        out, captured = self._run({"message_id": "AAMk=", "is_read": False}, {})
        self.assertTrue(out["success"])
        self.assertFalse(out["isRead"])
        self.assertEqual(captured["payload"], {"isRead": False})

    def test_missing_id_does_not_call_graph(self):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph") as graph,
        ):
            out = json.loads(self.plugin._mark_read_tool({}))
        self.assertFalse(out["success"])
        graph.assert_not_called()

    def test_a_truncated_known_id_is_completed_before_patching(self):
        real = "AAMkAGQ2" + "x" * 140 + "AAA="
        self.plugin._remember_ids([{"id": real}])
        out, captured = self._run({"message_id": real[:120]}, {})
        self.assertTrue(out["success"])
        self.assertEqual(captured["path"], f"/me/messages/{real}")

    def test_the_tool_is_registered_and_tells_the_agent_to_ask_first(self):
        ctx = MagicMock()
        self.plugin.register(ctx)
        schemas = {c.kwargs["name"]: c.kwargs["schema"] for c in ctx.register_tool.call_args_list}
        self.assertIn("ms365_mail_mark_read", schemas)
        description = schemas["ms365_mail_mark_read"]["description"]
        self.assertIn("PREGUNTA SIEMPRE", description)
        manifest = (
            Path(__file__).resolve().parents[1]
            / "hermeshq"
            / "integration_packages"
            / "ms365-mail"
            / "plugin"
            / "plugin.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("ms365_mail_mark_read", manifest)


class MalformedIdGuardTests(_Base):
    def _get(self, graph_result):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value=graph_result),
        ):
            return json.loads(self.plugin._get_email_tool({"message_id": "ROTO"}))

    def test_a_malformed_id_400_tells_the_model_to_relist_instead_of_retrying(self):
        out = self._get({"error": "Error 400: Id is malformed.", "_graph_code": "ErrorInvalidIdMalformed"})
        self.assertFalse(out["success"])
        self.assertIn("ms365_mail_list", out["error"])
        self.assertIn("No reintentes", out["error"])
        self.assertNotIn("malformed", out["error"].lower())

    def test_an_invalid_id_message_without_code_is_also_caught(self):
        out = self._get({"error": "Error 400: The Id is invalid."})
        self.assertIn("EXACTO", out["error"])

    def test_an_unrelated_error_is_left_untouched(self):
        out = self._get({"error": "Correo no encontrado (404)."})
        self.assertEqual(out["error"], "Correo no encontrado (404).")

    def test_mark_read_also_guards_malformed_ids(self):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value={"error": "Error 400: Id is malformed."}),
        ):
            out = json.loads(self.plugin._mark_read_tool({"message_id": "ROTO"}))
        self.assertFalse(out["success"])
        self.assertIn("ms365_mail_list", out["errores"][0]["error"])


class MarkReadBatchTests(_Base):
    def test_marks_several_ids_in_one_call(self):
        calls = []

        def fake_graph(method, path, token, payload=None, **kwargs):
            calls.append(path)
            return {}

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
        ):
            out = json.loads(self.plugin._mark_read_tool({"message_ids": ["A", "B", "C"]}))
        self.assertTrue(out["success"], out)
        self.assertEqual(len(out["marcados"]), 3)
        self.assertEqual(len(calls), 3)

    def test_reports_partial_failures_without_failing_the_rest(self):
        def fake_graph(method, path, token, payload=None, **kwargs):
            return {"error": "boom"} if path.endswith("/B") else {}

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
        ):
            out = json.loads(self.plugin._mark_read_tool({"message_ids": ["A", "B", "C"]}))
        self.assertFalse(out["success"])
        self.assertEqual(set(out["marcados"]), {"A", "C"})
        self.assertEqual(len(out["errores"]), 1)
        self.assertEqual(out["errores"][0]["message_id"], "B")


if __name__ == "__main__":
    unittest.main()
