import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("sharepoint_plugin_upload_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FolderNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def test_windows_onedrive_path_becomes_a_relative_drive_path(self):
        folder, error = self.plugin._normalize_drive_folder(
            r"C:\Users\usuario\OneDrive - Contoso\Escritorio\Usuario\Proyectos\IA"
        )
        self.assertIsNone(error)
        self.assertEqual(folder, "Escritorio/Usuario/Proyectos/IA")

    def test_plain_paths_are_cleaned_and_traversal_is_rejected(self):
        self.assertEqual(self.plugin._normalize_drive_folder("/Escritorio//Proyectos/")[0], "Escritorio/Proyectos")
        self.assertEqual(self.plugin._normalize_drive_folder("")[0], "")
        for malo in ("../otra", "a/../../b", "a/b:c", "x/" + "y/" * 11):
            self.assertIsNotNone(self.plugin._normalize_drive_folder(malo)[1], malo)


class UploadToolTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        (Path(self.tmp.name) / "outputs").mkdir()
        (Path(self.tmp.name) / "outputs" / "informe.docx").write_bytes(b"contenido")

    def _run(self, args, put_result=None):
        calls = []

        def fake_put(url, data, token=None, content_range=None):
            calls.append(url)
            return put_result or {"id": "1", "name": "informe.docx", "webUrl": "https://od/x"}

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.plugin, "_put_binary", side_effect=fake_put),
        ):
            return json.loads(self.plugin._upload_file_tool(args)), calls

    def test_uploads_into_the_requested_folder_without_overwriting(self):
        out, calls = self._run({"file_path": "outputs/informe.docx", "folder": "Escritorio/Usuario/IA"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["folder"], "Escritorio/Usuario/IA")
        self.assertIn("/me/drive/root:/Escritorio/Usuario/IA/informe.docx:/content", calls[0])
        self.assertIn("conflictBehavior=rename", calls[0])

    def test_files_outside_the_workspace_and_bad_folders_are_rejected(self):
        out, calls = self._run({"file_path": "/etc/passwd", "folder": "x"})
        self.assertFalse(out["success"])
        out, calls = self._run({"file_path": "outputs/informe.docx", "folder": "../x"})
        self.assertFalse(out["success"])
        self.assertEqual(calls, [])

    def test_permission_error_is_reported(self):
        out, _ = self._run(
            {"file_path": "outputs/informe.docx", "folder": "x"}, {"error": "Sin permiso para escribir (403)"}
        )
        self.assertFalse(out["success"])
        self.assertIn("403", out["error"])


class ListFilesPathTests(unittest.TestCase):
    def test_windows_paths_are_accepted_when_listing_onedrive(self):
        plugin = _load()
        pedidos = []

        def fake_graph(method, path, token):
            pedidos.append(path)
            return {"value": []}

        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(plugin, "_graph", side_effect=fake_graph),
        ):
            out = json.loads(
                plugin._list_files_tool({"folder_path": r"C:\Users\x\OneDrive - Contoso\Escritorio\Usuario"})
            )
        self.assertTrue(out["success"])
        self.assertEqual(pedidos[0], "/me/drive/root:/Escritorio/Usuario:/children")


class TreeAndLibraryTests(unittest.TestCase):
    def _run(self, args, respuestas):
        plugin = _load()
        pedidos = []

        def fake_graph(method, path, token, payload=None):
            pedidos.append(path)
            for fragmento, respuesta in respuestas:
                if fragmento in path:
                    return respuesta
            return {"error": "Error 404: Resource not found for the segment 'root:'."}

        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(plugin, "_graph", side_effect=fake_graph),
        ):
            return json.loads(plugin._list_files_tool(args)), pedidos

    def test_the_library_named_in_the_path_is_found_automatically(self):
        respuestas = [
            (
                "/drives?$select",
                {
                    "value": [
                        {"id": "dA", "name": "Documentos"},
                        {"id": "dB", "name": "Gerencia Administración Y Finanzas"},
                    ]
                },
            ),
            ("?$select=id", {"id": "site1"}),
            ("/drives/dB/root:/TI/10. SGSI:/children", {"value": [{"id": "1", "name": "01. Políticas", "folder": {}}]}),
        ]
        out, _ = self._run(
            {
                "site_url": "https://x.sharepoint.com/sites/U",
                "folder_path": "Gerencia Administración Y Finanzas/TI/10. SGSI",
            },
            respuestas,
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["biblioteca"], "Gerencia Administración Y Finanzas")
        self.assertEqual(out["items"][0]["name"], "01. Políticas")

    def test_depth_returns_the_tree_in_one_call(self):
        respuestas = [
            ("/me/drive/root:/Raiz/Sub:/children", {"value": [{"name": "a.docx", "size": 5}]}),
            (
                "/me/drive/root:/Raiz:/children",
                {"value": [{"name": "Sub", "folder": {}}, {"name": "b.pdf", "size": 9}]},
            ),
        ]
        out, _ = self._run({"folder_path": "Raiz", "depth": 2}, respuestas)
        self.assertTrue(out["success"], out)
        arbol = {n["name"]: n for n in out["tree"]}
        self.assertEqual(arbol["Sub"]["children"][0]["name"], "a.docx")
        self.assertEqual(arbol["b.pdf"]["type"], "file")
        self.assertEqual(out["count"], 3)

    def test_folders_only_tree_counts_files_and_skips_them(self):
        respuestas = [
            ("/me/drive/root:/Raiz/Sub:/children", {"value": [{"name": "a.docx"}, {"name": "Deep", "folder": {}}]}),
            ("/me/drive/root:/Raiz:/children", {"value": [{"name": "Sub", "folder": {}}, {"name": "b.pdf"}]}),
        ]
        out, _ = self._run({"folder_path": "Raiz", "depth": 3, "solo_carpetas": True}, respuestas)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["archivos_en_la_raiz"], 1)
        sub = out["tree"][0]
        self.assertEqual((sub["name"], sub["archivos"]), ("Sub", 1))
        self.assertEqual(sub["children"][0]["name"], "Deep")
        self.assertEqual(out["count"], 2)
        self.assertNotIn("b.pdf", json.dumps(out))

    def test_folders_only_tree_allows_four_levels_and_a_bigger_budget(self):
        respuestas = [
            ("/me/drive/root:/A/B/C/D:/children", {"value": []}),
            ("/me/drive/root:/A/B/C:/children", {"value": [{"name": "D", "folder": {}}]}),
            ("/me/drive/root:/A/B:/children", {"value": [{"name": "C", "folder": {}}]}),
            ("/me/drive/root:/A:/children", {"value": [{"name": "B", "folder": {}}]}),
        ]
        out, _ = self._run({"folder_path": "A", "depth": 9, "solo_carpetas": True, "max_items": 5000}, respuestas)
        self.assertTrue(out["success"], out)
        nivel1 = out["tree"][0]
        self.assertEqual(nivel1["name"], "B")
        self.assertEqual(nivel1["children"][0]["children"][0]["name"], "D")
