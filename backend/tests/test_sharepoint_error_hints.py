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

    def test_caso_real_itemnotfound_en_ingles_tambien_lleva_la_pista(self):
        crudo = "{'code': 'itemNotFound', 'message': 'The resource could not be found.'}"
        self.assertIn("sharepoint_search", sp._with_hint(crudo))
        self.assertIn("sharepoint_search", sp._with_hint("The resource could not be found."))

    def test_hint_is_not_added_twice(self):
        once = sp._with_hint("Archivo o carpeta no encontrado (404).")
        self.assertEqual(sp._with_hint(once), once)


class TestNoEncontradoNoEsRestriccion(unittest.TestCase):
    def test_caso_real_item_no_resuelto_no_se_presenta_como_restriccion(self):
        res = sp._error_de_politica("item_no_resuelto")
        self.assertEqual(res["_graph_code"], "not_found")
        self.assertIn("No es una restricción", res["error"])
        self.assertIn("sharepoint_search", res["error"])
        self.assertNotIn("Acceso denegado", res["error"])

    def test_una_restriccion_real_se_sigue_informando_como_tal(self):
        res = sp._error_de_politica("unidad_fuera_de_alcance")
        self.assertEqual(res["_graph_code"], "policy_denied")
        self.assertIn("Acceso denegado por la política de datos", res["error"])
        self.assertIn("administrador", res["error"])

    def test_el_broker_que_rechaza_por_item_no_resuelto_llega_como_no_encontrado(self):
        import io
        import urllib.error

        cuerpo = io.BytesIO(json.dumps({"detail": {"motivo": "item_no_resuelto"}}).encode())
        error = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, cuerpo)
        with (
            patch.object(sp, "_broker_headers", return_value=("http://x", {}, "t1")),
            patch.object(sp.urllib.request, "urlopen", side_effect=error),
        ):
            res = sp._broker_request("GET", "/drives/d/items/i/content")
        self.assertEqual(res["_graph_code"], "not_found")
