import json
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load

TOP = {
    "value": [
        {
            "id": "ID-INBOX",
            "displayName": "Bandeja de entrada",
            "childFolderCount": 2,
            "unreadItemCount": 3,
            "totalItemCount": 40,
        },
        {
            "id": "ID-SENT",
            "displayName": "Elementos enviados",
            "childFolderCount": 0,
            "unreadItemCount": 0,
            "totalItemCount": 9,
        },
    ]
}
CHILDREN = {
    "value": [
        {
            "id": "ID-INFO",
            "displayName": "Informaciones",
            "childFolderCount": 0,
            "unreadItemCount": 1,
            "totalItemCount": 7,
        },
        {"id": "ID-PROY", "displayName": "Proyectos", "childFolderCount": 0, "unreadItemCount": 0, "totalItemCount": 2},
    ]
}


def _fake_graph(method, path, token, **kwargs):
    if "/childFolders" in path:
        return CHILDREN
    if path.startswith("/me/mailFolders?"):
        return TOP
    return {"value": []}


class TestMailFolders(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _resolve(self, name):
        with patch.object(self.mod, "_graph", side_effect=_fake_graph):
            return self.mod._resolve_folder("tok", name)

    def test_well_known_and_aliases_skip_graph(self):
        with patch.object(self.mod, "_graph", side_effect=AssertionError("no debe llamar a Graph")):
            self.assertEqual(self.mod._resolve_folder("tok", "inbox"), ("inbox", None))
            self.assertEqual(self.mod._resolve_folder("tok", "Bandeja de entrada"), ("inbox", None))
            self.assertEqual(self.mod._resolve_folder("tok", "Enviados"), ("sentitems", None))

    def test_custom_subfolder_by_name_ignores_case_and_accents(self):
        self.assertEqual(self._resolve("informaciónes"), ("ID-INFO", None))
        self.assertEqual(self._resolve("INFORMACIONES"), ("ID-INFO", None))

    def test_custom_subfolder_by_path(self):
        self.assertEqual(self._resolve("Bandeja de entrada/Proyectos"), ("ID-PROY", None))

    def test_unknown_folder_lists_available(self):
        folder, error = self._resolve("Inexistente")
        self.assertIsNone(folder)
        self.assertIn("Bandeja de entrada/Informaciones", error)

    def test_long_id_passes_through(self):
        folder_id = "AAMkAGI2" + "x" * 60
        self.assertEqual(self._resolve(folder_id), (folder_id, None))

    def test_list_uses_resolved_folder_id(self):
        calls = []

        def graph(method, path, token, **kwargs):
            calls.append(path)
            return _fake_graph(method, path, token, **kwargs)

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            out = json.loads(self.mod._list_emails_tool({"folder": "Informaciones", "count": 5}))
        self.assertTrue(out["success"])
        self.assertTrue(any("/me/mailFolders/ID-INFO/messages" in c for c in calls))

    def test_folders_tool_returns_paths(self):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=_fake_graph),
        ):
            out = json.loads(self.mod._list_folders_tool({}))
        paths = [f["path"] for f in out["folders"]]
        self.assertIn("Bandeja de entrada/Informaciones", paths)
