import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("sharepoint_plugin_error_hints", _PLUGIN)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


def _run(tool, args, graph_result):
    with (
        patch.object(sp, "_task_user_id", return_value="u1"),
        patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
        patch.object(sp, "_graph", return_value=graph_result),
    ):
        return json.loads(tool(args))


class TestSharepointErrorHints(unittest.TestCase):
    def test_not_found_on_guessed_path_tells_the_agent_to_search(self):
        out = _run(
            sp._get_file_tool,
            {"file_path": "Documentos/Inventado.pdf"},
            {"error": "Archivo o carpeta no encontrado (404)."},
        )
        self.assertFalse(out["success"])
        self.assertIn("sharepoint_search", out["error"])
        self.assertIn("item_id", out["error"])

    def test_root_segment_error_gets_the_hint(self):
        out = _run(
            sp._list_files_tool,
            {"site_url": "https://x.sharepoint.com/sites/NoExiste"},
            {"error": "Error 400: Resource not found for the segment 'root:'."},
        )
        self.assertIn("sharepoint_list_drives", out["error"])

    def test_download_by_guessed_ids_gets_the_hint(self):
        out = _run(
            sp._download_file_tool,
            {"drive_id": "b!x", "item_id": "FAKE"},
            {"error": "Error 400: Invalid request"},
        )
        self.assertIn("sharepoint_search", out["error"])

    def test_unrelated_error_is_left_alone(self):
        out = _run(sp._get_file_tool, {"file_path": "a.pdf"}, {"error": "Límite de peticiones alcanzado."})
        self.assertNotIn("Pista", out["error"])

    def test_hint_is_not_added_twice(self):
        once = sp._with_hint("Archivo o carpeta no encontrado (404).")
        self.assertEqual(sp._with_hint(once), once)
