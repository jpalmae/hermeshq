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
    spec = importlib.util.spec_from_file_location("sharepoint_plugin_folder_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file(name, size=100, url=True, item_id=None):
    item = {"name": name, "size": size, "id": item_id or f"id-{name}", "parentReference": {"driveId": "d1"}}
    if url:
        item["@microsoft.graph.downloadUrl"] = f"https://download.example.com/{name}"
    return item


def _folder(name, item_id=None):
    return {"name": name, "folder": {}, "id": item_id or f"id-{name}", "parentReference": {"driveId": "d1"}}


class DownloadFolderTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        anterior = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, anterior)
        self.pedidos = []
        self.descargas = []

    def _run(self, args, carpetas):
        def fake_graph(method, path, token, payload=None):
            self.pedidos.append(path)
            for fragmento, respuesta in carpetas:
                if fragmento in path:
                    return respuesta
            return {"error": "Error 404: not found"}

        def fake_download(url, access_token=None):
            self.descargas.append(url)
            if "boom" in url:
                return None, "Error HTTP 500 al descargar el archivo."
            return url.encode("utf-8"), ""

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
            patch.object(self.plugin, "_download_bytes", side_effect=fake_download),
        ):
            return json.loads(self.plugin._download_folder_tool(args))

    def test_every_file_of_the_folder_is_saved_in_one_call(self):
        carpetas = [("/me/drive/root:/Ref:/children", {"value": [_file("a.png"), _file("b.pdf"), _folder("sub")]})]
        out = self._run({"folder_path": "Ref"}, carpetas)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["descargados"], 2)
        self.assertEqual(sorted(f["name"] for f in out["archivos"]), ["a.png", "b.pdf"])
        for archivo in out["archivos"]:
            self.assertTrue((Path(self.tmp.name) / archivo["local_path"]).is_file())
            self.assertTrue(archivo["local_path"].startswith("sharepoint_downloads/Ref/"))
        self.assertEqual(len(self.descargas), 2)

    def test_extensions_filter_what_is_downloaded(self):
        carpetas = [("/me/drive/root:/Ref:/children", {"value": [_file("a.png"), _file("b.pdf"), _file("c.PNG")]})]
        out = self._run({"folder_path": "Ref", "extensions": ["png", ".jpg"]}, carpetas)
        self.assertEqual(sorted(f["name"] for f in out["archivos"]), ["a.png", "c.PNG"])
        self.assertEqual(out["omitidos"], [])

    def test_subfolders_are_only_entered_when_asked(self):
        carpetas = [
            ("/me/drive/root:/Ref/sub:/children", {"value": [_file("dentro.png")]}),
            ("/me/drive/root:/Ref:/children", {"value": [_file("fuera.png"), _folder("sub")]}),
        ]
        plano = self._run({"folder_path": "Ref"}, carpetas)
        self.assertEqual([f["name"] for f in plano["archivos"]], ["fuera.png"])
        recursivo = self._run({"folder_path": "Ref", "recursive": True}, carpetas)
        self.assertEqual(sorted(f["name"] for f in recursivo["archivos"]), ["dentro.png", "fuera.png"])
        dentro = next(f for f in recursivo["archivos"] if f["name"] == "dentro.png")
        self.assertEqual(dentro["subcarpeta"], "sub")
        self.assertIn("sub__dentro.png", dentro["local_path"])

    def test_limits_are_reported_instead_of_failing(self):
        archivos = [_file(f"f{i}.png", size=1024) for i in range(5)] + [_file("grande.pdf", size=11 * 1024 * 1024)]
        carpetas = [("/me/drive/root:/Ref:/children", {"value": archivos})]
        out = self._run({"folder_path": "Ref", "max_files": 3}, carpetas)
        self.assertEqual(out["descargados"], 3)
        motivos = {o["name"]: o["motivo"] for o in out["omitidos"]}
        self.assertIn("10 MB", motivos["grande.pdf"])
        self.assertIn("máximo de 3 archivos", motivos["f3.png"])

    def test_total_size_budget_is_enforced(self):
        archivos = [_file("a.bin", size=6 * 1024 * 1024), _file("b.bin", size=6 * 1024 * 1024)]
        carpetas = [("/me/drive/root:/Ref:/children", {"value": archivos})]
        out = self._run({"folder_path": "Ref", "max_mb": 8}, carpetas)
        self.assertEqual(out["descargados"], 1)
        self.assertIn("8 MB en total", out["omitidos"][0]["motivo"])

    def test_a_failed_download_is_listed_and_does_not_stop_the_rest(self):
        archivos = [_file("ok.png"), {**_file("roto.png"), "@microsoft.graph.downloadUrl": "https://x/boom.png"}]
        carpetas = [("/me/drive/root:/Ref:/children", {"value": archivos})]
        out = self._run({"folder_path": "Ref"}, carpetas)
        self.assertEqual([f["name"] for f in out["archivos"]], ["ok.png"])
        self.assertEqual(out["omitidos"][0]["name"], "roto.png")
        self.assertIn("500", out["omitidos"][0]["motivo"])

    def test_a_folder_can_be_given_by_item_id_and_files_without_a_download_url_use_graph(self):
        carpetas = [("/drives/d1/items/F1/children", {"value": [_file("x.png", url=False, item_id="X1")]})]
        out = self._run({"item_id": "F1", "drive_id": "d1"}, carpetas)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["descargados"], 1)
        self.assertIn("/drives/d1/items/X1/content", self.descargas[0])

    def test_errors_are_actionable(self):
        self.assertIn("folder_path", self._run({}, [])["error"])
        r = self._run({"folder_path": "NoExiste"}, [])
        self.assertFalse(r["success"])

    def test_the_tool_is_registered_and_listed_in_the_manifest(self):
        class Ctx:
            def __init__(self):
                self.nombres = []

            def register_tool(self, **kwargs):
                self.nombres.append(kwargs["name"])

        ctx = Ctx()
        self.plugin.register(ctx)
        self.assertIn("sharepoint_download_folder", ctx.nombres)
        self.assertIn("sharepoint_download_folder", PLUGIN_PATH.with_name("plugin.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
