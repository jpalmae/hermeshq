import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from _sp_busqueda import adaptar_busqueda

_PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("sharepoint_plugin_item_id_listing", _PLUGIN)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


class ListByItemIdTests(unittest.TestCase):
    def test_list_files_by_item_id_addresses_the_item_not_onedrive_root(self):
        calls = []

        def fake_graph(method, path, token, payload=None, **kwargs):
            calls.append(path)
            return {"value": [{"id": "f1", "name": "doc.pdf", "size": 10}]}

        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(sp, "_graph", side_effect=fake_graph),
        ):
            out = json.loads(sp._list_files_tool({"item_id": "01ITEM", "drive_id": "b!d"}))
        self.assertTrue(out["success"], out)
        self.assertEqual(calls, ["/drives/b!d/items/01ITEM/children"])
        self.assertEqual([i["name"] for i in out["items"]], ["doc.pdf"])

    def test_item_id_without_drive_id_falls_back_to_me_drive_items(self):
        calls = []

        def fake_graph(method, path, token, payload=None, **kwargs):
            calls.append(path)
            return {"value": []}

        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(sp, "_graph", side_effect=fake_graph),
        ):
            json.loads(sp._list_files_tool({"item_id": "01ITEM"}))
        self.assertEqual(calls, ["/me/drive/items/01ITEM/children"])


class NonRetryHintTests(unittest.TestCase):
    def test_a_not_found_path_returns_candidates_and_a_non_retry_instruction(self):
        candidate = {
            "id": "01ABC",
            "name": "Politica-SGSI.pdf",
            "webUrl": "https://x.sharepoint.com/f.pdf",
            "parentReference": {"driveId": "b!drive", "path": "/drive/root:/SGSI"},
        }

        def fake_graph(method, path, token, payload=None, **kwargs):
            if path == "/search/query":
                return {"value": [{"hitsContainers": [{"hits": [{"resource": candidate}]}]}]}
            return {"error": "Archivo o carpeta no encontrado (404)."}

        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(sp, "_graph", side_effect=adaptar_busqueda(fake_graph)),
        ):
            out = json.loads(sp._get_file_tool({"file_path": "Inventada/Politica-SGSI.pdf"}))
        self.assertFalse(out["success"])
        self.assertEqual(out["candidatos"][0]["item_id"], "01ABC")
        self.assertIn("NO la reintentes", out["siguiente"])
        self.assertIn("sharepoint_list_files", out["siguiente"])


if __name__ == "__main__":
    unittest.main()
