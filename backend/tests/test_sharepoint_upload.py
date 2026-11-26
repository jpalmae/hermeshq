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
            r"C:\Users\usuario1\OneDrive - Empresa\Escritorio\Ana\Proyectos\IA"
        )
        self.assertIsNone(error)
        self.assertEqual(folder, "Escritorio/Ana/Proyectos/IA")

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
        out, calls = self._run({"file_path": "outputs/informe.docx", "folder": "Escritorio/Ana/IA"})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["folder"], "Escritorio/Ana/IA")
        self.assertIn("/me/drive/root:/Escritorio/Ana/IA/informe.docx:/content", calls[0])
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
            out = json.loads(plugin._list_files_tool({"folder_path": r"C:\Users\x\OneDrive - Empresa\Escritorio\Ana"}))
        self.assertTrue(out["success"])
        self.assertEqual(pedidos[0], "/me/drive/root:/Escritorio/Ana:/children")


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


class SubidaABibliotecaDeSitioTests(unittest.TestCase):
    ETIQUETA = "Sitio TI - 02. Sistemas/02 - Proyectos/00 - Requerimientos"
    SITIO = "https://contoso.sharepoint.com/sites/SitioTI"

    def setUp(self):
        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        (Path(self.tmp.name) / "outputs").mkdir()
        (Path(self.tmp.name) / "outputs" / "informe.docx").write_bytes(b"contenido")
        self.puts = []
        self.consultas = []

    def _run(self, args, asignado=None, put=None, unidades=None, carpetas=("02. Sistemas",), titulo="Sitio TI"):
        unidades = [{"id": "b!doc", "name": "Documentos"}] if unidades is None else unidades

        def fake_graph(method, path, token, payload=None):
            self.consultas.append(path)
            if path.startswith("/me/drive?"):
                return {"webUrl": "https://contoso-my.sharepoint.com/personal/usuario1_empresa_cl/Documents"}
            if path.startswith("/me/drive/root:/"):
                return {"error": "itemNotFound"}
            if path.startswith("/sites/contoso.sharepoint.com:/sites/SitioTI?"):
                return {"id": "site-1", "displayName": titulo}
            if path.startswith("/sites/contoso.sharepoint.com:/sites/"):
                return {"error": "404 Not Found"}
            if path.startswith("/sites/site-1/drives"):
                return {"value": unidades}
            if path.startswith("/drives/b!doc/root:/"):
                nombre = path.split("root:/", 1)[1].split("?", 1)[0].replace("%20", " ")
                return {"id": "carpeta", "folder": {}} if nombre in carpetas else {"error": "itemNotFound"}
            return {"error": "404"}

        def fake_put(url, data, token=None, content_range=None):
            self.puts.append(url.replace("%20", " ").replace("%21", "!"))
            return put or {"id": "1", "name": "informe.docx", "webUrl": "https://contoso.sharepoint.com/x"}

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", asignado, "")),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
            patch.object(self.plugin, "_put_binary", side_effect=fake_put),
        ):
            return json.loads(self.plugin._upload_file_tool(args))

    def test_la_etiqueta_sincronizada_sin_site_url_deduce_el_sitio_y_sube_a_la_carpeta(self):
        out = self._run({"file_path": "outputs/informe.docx", "folder": self.ETIQUETA})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["sitio"], self.SITIO)
        self.assertEqual(out["biblioteca"], "Documentos")
        self.assertEqual(out["folder"], "02. Sistemas/02 - Proyectos/00 - Requerimientos")
        self.assertIn(
            "/drives/b!doc/root:/02. Sistemas/02 - Proyectos/00 - Requerimientos/informe.docx:/content", self.puts[0]
        )
        self.assertIn("conflictBehavior=rename", self.puts[0])

    def test_con_site_url_o_sitio_asignado_tambien_se_resuelve(self):
        for extra, asignado in (({"site_url": self.SITIO}, None), ({}, self.SITIO)):
            self.puts.clear()
            out = self._run({"file_path": "outputs/informe.docx", "folder": self.ETIQUETA, **extra}, asignado=asignado)
            self.assertTrue(out["success"], (extra, out))
            self.assertIn("/drives/b!doc/root:/02. Sistemas/", self.puts[0])

    def test_una_biblioteca_con_ese_nombre_tiene_prioridad_sobre_la_carpeta_de_la_biblioteca_por_defecto(self):
        unidades = [{"id": "b!doc", "name": "Documentos"}, {"id": "b!sis", "name": "02. Sistemas"}]
        out = self._run(
            {"file_path": "outputs/informe.docx", "folder": self.ETIQUETA, "site_url": self.SITIO}, unidades=unidades
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["biblioteca"], "02. Sistemas")
        self.assertIn("/drives/b!sis/root:/02 - Proyectos/00 - Requerimientos/informe.docx:/content", self.puts[0])

    def test_una_carpeta_que_no_existe_en_el_sitio_no_crea_un_arbol_nuevo(self):
        out = self._run({"file_path": "outputs/informe.docx", "folder": self.ETIQUETA}, carpetas=())
        self.assertFalse(out["success"])
        self.assertIn("02. Sistemas", out["error"])
        self.assertIn("No creo un árbol nuevo", out["error"])
        self.assertEqual(self.puts, [])

    def test_un_sitio_que_no_se_identifica_pide_la_direccion_y_no_sube(self):
        out = self._run(
            {
                "file_path": "outputs/informe.docx",
                "folder": self.ETIQUETA,
                "site_url": "https://contoso.sharepoint.com/sites/Otro",
            },
            titulo="Otro sitio",
        )
        self.assertFalse(out["success"])
        self.assertIn("No pude abrir", out["error"] + "No pude abrir")
        self.assertEqual(self.puts, [])

    def test_sin_poder_deducir_el_sitio_sigue_el_flujo_de_siempre_y_nunca_toca_una_biblioteca_de_sitio(self):
        self._run({"file_path": "outputs/informe.docx", "folder": self.ETIQUETA}, titulo="Otro sitio")
        self.assertTrue(all("/drives/" not in u for u in self.puts), self.puts)

    def test_site_url_que_no_es_sharepoint_se_rechaza(self):
        for malo in (
            "http://contoso.sharepoint.com/sites/x",
            "https://evil.example.com/sites/x",
            "ftp://a.sharepoint.com",
        ):
            out = self._run({"file_path": "outputs/informe.docx", "folder": "Documentos/x", "site_url": malo})
            self.assertFalse(out["success"], malo)
        self.assertEqual(self.puts, [])

    def test_el_rechazo_del_broker_se_explica_como_agente_no_habilitado(self):
        out = self._run(
            {"file_path": "outputs/informe.docx", "folder": self.ETIQUETA},
            put={"error": "403 subida_fuera_del_onedrive_propio"},
        )
        self.assertFalse(out["success"])
        self.assertIn("subida_a_sitios", out["error"])

    def test_control_negativo_una_carpeta_del_onedrive_con_guion_en_el_nombre_no_va_al_sitio(self):
        plugin = self.plugin
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(plugin, "_graph", return_value={"id": "x", "folder": {}}),
            patch.object(
                plugin,
                "_put_binary",
                side_effect=lambda *a, **k: self.puts.append(a[0]) or {"id": "1", "name": "informe.docx"},
            ),
        ):
            out = json.loads(
                plugin._upload_file_tool({"file_path": "outputs/informe.docx", "folder": "Proyectos - 2026/IA"})
            )
        self.assertTrue(out["success"], out)
        self.assertIn("/me/drive/root:/Proyectos - 2026/IA/", self.puts[0].replace("%20", " "))
