import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ms365_mail_list_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ListEmailsTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _list(self, args, graph_result=None):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value=graph_result or {"value": []}) as graph,
        ):
            result = json.loads(self.plugin._list_emails_tool(args))
        return result, graph

    def test_default_list_has_no_filter(self):
        result, graph = self._list({"count": 5})
        self.assertTrue(result["success"])
        path = graph.call_args.args[1]
        self.assertNotIn("$filter", path)
        self.assertIn("$top=5", path)

    def test_since_is_converted_to_utc_filter(self):
        _, graph = self._list({"since": "2026-10-06T17:00:00-03:00"})
        self.assertIn("$filter=receivedDateTime ge 2026-10-06T20:00:00Z", graph.call_args.args[1])

    def test_since_accepts_z_suffix_and_naive_as_utc(self):
        _, graph = self._list({"since": "2026-10-06T20:00:00Z"})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z", graph.call_args.args[1])
        _, graph = self._list({"since": "2026-10-06T20:00:00"})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z", graph.call_args.args[1])

    def test_unread_only_filters_by_date_first_as_graph_requires(self):
        _, graph = self._list({"unread_only": True})
        path = graph.call_args.args[1]
        self.assertIn("$filter=receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false", path)
        self.assertLess(path.index("$filter"), path.index("$orderby"))

    def test_since_and_unread_only_combine(self):
        _, graph = self._list({"since": "2026-10-06T20:00:00Z", "unread_only": True})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z and isRead eq false", graph.call_args.args[1])

    def test_invalid_since_is_rejected_without_calling_graph(self):
        result, graph = self._list({"since": "ayer a las ocho"})
        self.assertFalse(result["success"])
        graph.assert_not_called()

    def test_previews_are_shortened(self):
        long_preview = "x" * 250
        result, _ = self._list(
            {}, {"value": [{"id": "1", "bodyPreview": long_preview}, {"id": "2", "bodyPreview": "corto"}]}
        )
        self.assertEqual(len(result["messages"][0]["bodyPreview"]), 201)
        self.assertTrue(result["messages"][0]["bodyPreview"].endswith("…"))
        self.assertEqual(result["messages"][1]["bodyPreview"], "corto")

    def test_preview_chars_is_configurable_and_capped(self):
        result, _ = self._list({"preview_chars": 50}, {"value": [{"id": "1", "bodyPreview": "y" * 250}]})
        self.assertEqual(len(result["messages"][0]["bodyPreview"]), 51)
        result, _ = self._list({"preview_chars": 9999}, {"value": [{"id": "1", "bodyPreview": "z" * 255}]})
        self.assertEqual(len(result["messages"][0]["bodyPreview"]), 255)

    def test_schema_documents_new_parameters(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        self.plugin.register(ctx)
        schemas = {c.kwargs["name"]: c.kwargs["schema"] for c in ctx.register_tool.call_args_list}
        properties = schemas["ms365_mail_list"]["parameters"]["properties"]
        for name in ("since", "unread_only", "preview_chars", "count", "folder"):
            self.assertIn(name, properties)


if __name__ == "__main__":
    unittest.main()
