import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("sharepoint_plugin_search_ids", _PLUGIN)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


def _search_response(resources):
    return {"value": [{"hitsContainers": [{"hits": [{"resource": r} for r in resources]}]}]}


class SearchIdsTests(unittest.TestCase):
    def _search(self, resources):
        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(sp, "_graph", return_value=_search_response(resources)),
        ):
            return json.loads(sp._search_tool({"query": "SGSI"}))

    def test_hits_carry_item_and_drive_ids(self):
        result = self._search(
            [
                {
                    "id": "01ABC",
                    "name": "SGSI-REG-GSI-02.xlsx",
                    "webUrl": "https://x.sharepoint.com/f.xlsx",
                    "lastModifiedDateTime": "2026-10-06T18:14:00Z",
                    "size": 40960,
                    "parentReference": {"driveId": "b!drive", "siteId": "s"},
                }
            ]
        )
        hit = result["hits"][0]
        self.assertEqual(hit["item_id"], "01ABC")
        self.assertEqual(hit["drive_id"], "b!drive")
        self.assertEqual(hit["name"], "SGSI-REG-GSI-02.xlsx")
        self.assertEqual(hit["size"], 40960)

    def test_missing_parent_reference_does_not_break_the_search(self):
        result = self._search([{"id": "01ABC", "name": "a.docx"}, {"name": "b.docx", "parentReference": None}])
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertIsNone(result["hits"][0]["drive_id"])
        self.assertIsNone(result["hits"][1]["item_id"])

    def test_search_ids_are_enough_to_read_a_file_without_path_or_site(self):
        calls = []

        def fake_graph(method, path, token, payload=None, **kwargs):
            calls.append(path)
            return {"name": "doc.txt", "size": 5, "webUrl": "https://x/doc.txt"}

        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", "https://x.sharepoint.com/sites/y", "")),
            patch.object(sp, "_graph", side_effect=fake_graph),
            patch.object(sp, "_download_bytes", return_value=(b"hello", "")),
        ):
            error, info = sp._fetch_item({"item_id": "01ABC", "drive_id": "b!drive"})
        self.assertIsNone(error)
        self.assertEqual(calls, ["/drives/b!drive/items/01ABC"])
        self.assertEqual(info["data"], b"hello")


if __name__ == "__main__":
    unittest.main()
