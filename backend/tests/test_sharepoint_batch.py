import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from _sp_busqueda import adaptar_busqueda

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("sharepoint_plugin_batch_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def test_search_accepts_several_terms_in_one_call(self):
        llamadas = []

        def falsa(args, **_):
            llamadas.append(args["query"])
            return json.dumps({"success": True, "results": [{"name": args["query"] + ".docx"}]})

        with patch.object(self.plugin, "_search_tool", side_effect=falsa):
            r = json.loads(self.plugin._search_multi_tool({"queries": ["alfa", " ", "beta"], "count": 3}))
        self.assertEqual(llamadas, ["alfa", "beta"])
        self.assertTrue(r["success"])
        self.assertEqual([b["consulta"] for b in r["busquedas"]], ["alfa", "beta"])

    def test_a_single_query_still_uses_the_original_path(self):
        with patch.object(self.plugin, "_search_tool", return_value='{"success": true, "results": []}') as una:
            self.plugin._search_multi_tool({"query": "solo"})
        una.assert_called_once()

    def test_read_accepts_several_files_and_caps_each_one(self):
        recibidos = []

        def falsa(args, **_):
            recibidos.append(args)
            return json.dumps({"success": True, "name": args.get("item_id"), "text": "x"})

        items = [{"item_id": f"i{n}", "drive_id": "d"} for n in range(8)]
        with patch.object(self.plugin, "_read_file_tool", side_effect=falsa):
            r = json.loads(self.plugin._read_multi_tool({"items": items}))
        self.assertEqual(len(r["archivos"]), 5)
        self.assertTrue(all(a["max_chars"] == 12000 for a in recibidos))
        self.assertEqual([a["item_id"] for a in recibidos], [f"i{n}" for n in range(5)])

    def test_download_accepts_several_files_and_reports_each_error(self):
        def falsa(args, **_):
            if args.get("item_id") == "malo":
                return json.dumps({"success": False, "error": "no existe"})
            return json.dumps({"success": True, "local_path": "sharepoint_downloads/a.docx"})

        with patch.object(self.plugin, "_download_file_tool", side_effect=falsa):
            r = json.loads(self.plugin._download_multi_tool({"items": [{"item_id": "ok"}, {"item_id": "malo"}]}))
        self.assertTrue(r["success"])
        self.assertEqual([a["success"] for a in r["archivos"]], [True, False])

    def test_the_tools_advertise_the_batch_parameters(self):
        class Ctx:
            def __init__(self):
                self.esquemas = {}

            def register_tool(self, **kwargs):
                self.esquemas[kwargs["name"]] = kwargs["schema"]["parameters"]["properties"]

        ctx = Ctx()
        self.plugin.register(ctx)
        self.assertIn("queries", ctx.esquemas["sharepoint_search"])
        self.assertIn("items", ctx.esquemas["sharepoint_read_file"])
        self.assertIn("items", ctx.esquemas["sharepoint_download_file"])


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _graph(self, ruta_error):
        def falsa(method, path, token, payload=None):
            if method == "POST" and path == "/search/query":
                return {
                    "value": [
                        {
                            "hitsContainers": [
                                {
                                    "hits": [
                                        {
                                            "resource": {
                                                "id": "I1",
                                                "name": "Informe_V3.html",
                                                "webUrl": "https://example.com/i",
                                                "parentReference": {
                                                    "driveId": "D1",
                                                    "path": "/drive/root:/Proyectos/BI",
                                                },
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            return {"error": ruta_error}

        return falsa

    def test_a_wrong_path_comes_back_with_the_likely_files(self):
        error = "Error 400: Resource not found for the segment 'root:'."
        with patch.object(self.plugin, "_graph", side_effect=adaptar_busqueda(self._graph(error))):
            r = json.loads(self.plugin._failure_with_candidates(error, "Escritorio/Proyectos/Informe_V3.html", "tok"))
        self.assertFalse(r["success"])
        self.assertEqual(r["candidatos"][0]["item_id"], "I1")
        self.assertEqual(r["candidatos"][0]["folder"], "Proyectos/BI")
        self.assertIn("no pruebes más rutas", r["siguiente"])

    def test_other_errors_and_tiny_names_get_no_search(self):
        with patch.object(self.plugin, "_graph") as grafo:
            r = json.loads(self.plugin._failure_with_candidates("Error 500: caída", "a/b/Informe.html", "tok"))
            self.assertNotIn("candidatos", r)
            self.plugin._failure_with_candidates("404 no encontrado", "a/b", "tok")
        grafo.assert_not_called()

    def test_the_lookup_error_of_a_file_read_carries_the_candidates(self):
        error = "Error 404: no encontrado"
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "", None)),
            patch.object(self.plugin, "_graph", side_effect=adaptar_busqueda(self._graph(error))),
        ):
            r = json.loads(self.plugin._read_file_tool({"file_path": "Carpeta/Informe_V3.html"}))
        self.assertEqual(r["candidatos"][0]["name"], "Informe_V3.html")


if __name__ == "__main__":
    unittest.main()
