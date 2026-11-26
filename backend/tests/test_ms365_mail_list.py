import importlib.util
import json
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ms365_mail_list_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _alinear_costuras_broker(module)


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

    @staticmethod
    def _path(graph):
        return urllib.parse.unquote(graph.call_args.args[1])

    def test_default_list_has_no_filter(self):
        result, graph = self._list({"count": 5})
        self.assertTrue(result["success"])
        path = graph.call_args.args[1]
        self.assertNotIn("$filter", path)
        self.assertIn("$top=5", path)

    def test_caso_real_since_en_el_futuro_se_rechaza(self):
        from datetime import UTC, datetime, timedelta

        futuro = (datetime.now(UTC) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result, graph = self._list({"since": futuro, "unread_only": True})
        self.assertFalse(result["success"])
        self.assertIn("futuro", result["error"])
        graph.assert_not_called()
        ayer = (datetime.now(UTC) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
        result, _ = self._list({"since": ayer})
        self.assertTrue(result["success"], result)

    def test_since_is_converted_to_utc_filter(self):
        _, graph = self._list({"since": "2026-10-06T17:00:00-03:00"})
        self.assertIn("$filter=receivedDateTime ge 2026-10-06T20:00:00Z", self._path(graph))

    def test_since_accepts_z_suffix_and_naive_as_chile(self):
        _, graph = self._list({"since": "2026-10-06T20:00:00Z"})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z", self._path(graph))
        _, graph = self._list({"since": "2026-10-06T17:00:00"})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z", self._path(graph))
        _, graph = self._list({"since": "2026-07-06T17:00:00"})
        self.assertIn("receivedDateTime ge 2026-07-06T21:00:00Z", self._path(graph), "invierno: -04:00")

    def test_el_resultado_dice_la_ventana_en_hora_de_chile(self):
        result, _ = self._list({"since": "2026-11-11T23:00:00Z", "unread_only": True})
        self.assertEqual(result["desde_chile"], "11-11-2026 20:00 (hora de Chile)")
        result, _ = self._list({})
        self.assertNotIn("desde_chile", result)

    def test_since_futuro_sin_zona_dice_la_hora_de_chile(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        futuro = (datetime.now(ZoneInfo("America/Santiago")) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
        result, graph = self._list({"since": futuro})
        self.assertFalse(result["success"])
        self.assertIn("hora de Chile", result["error"])
        graph.assert_not_called()
        pasado = (datetime.now(ZoneInfo("America/Santiago")) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
        result, _ = self._list({"since": pasado})
        self.assertTrue(result["success"], result)

    def test_unread_only_filters_by_date_first_as_graph_requires(self):
        _, graph = self._list({"unread_only": True})
        path = self._path(graph)
        self.assertIn("$filter=receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false", path)
        self.assertNotIn("$orderby", path)
        from hermeshq.services import graph_proxy as gp

        llamadas = []

        def red(metodo, url, token, cuerpo, tipo, limite):
            llamadas.append(urllib.parse.unquote(url))
            return 200, b'{"value": []}'

        politica = gp.PoliticaGraph(
            correo=gp.PoliticaCorreo(operaciones=frozenset({"mail_list"}), carpetas=frozenset({"inbox"}))
        )
        with patch.object(gp, "_llamar", red):
            gp.ejecutar("GET", graph.call_args.args[1], None, None, politica, lambda: "tok", lambda t: None)
        self.assertIn("$orderby=receivedDateTime desc", llamadas[0])
        self.assertIn("$filter=receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false", llamadas[0])

    def test_since_and_unread_only_combine(self):
        _, graph = self._list({"since": "2026-10-06T20:00:00Z", "unread_only": True})
        self.assertIn("receivedDateTime ge 2026-10-06T20:00:00Z and isRead eq false", self._path(graph))

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


def _alinear_costuras_broker(module):
    module._broker_token_check = lambda user_id: module._get_m365_token(user_id)
    module._broker_graph = lambda metodo, ruta, cuerpo_json=None, prefer=None: module._graph(
        metodo,
        ruta,
        "tok",
        **({"payload": cuerpo_json} if cuerpo_json is not None else {}),
        **({"headers": {"Prefer": prefer}} if prefer else {}),
    )
    return module
