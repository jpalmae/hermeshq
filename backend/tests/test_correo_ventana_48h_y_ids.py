import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_ventana_48h_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

MARTES_19H_UTC = datetime(2026, 11, 10, 19, 0, 3, tzinfo=UTC)
MID = "AQMkADAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMABGAAADbDNz7ba9ukaploOcsbVKZQcAiUd0wjrXV0SmQwNeFySSGAAAAgEMAAAAiUd0wjrXV0SmQwNeFySSGAAHRX-pSAAAAA=="


class VentanaCronSinMarcaEs48Horas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_primera_corrida_sin_marca_mira_48_horas(self):
        v = plugin._cu_ventana({"ahora": MARTES_19H_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.assertEqual(v["since"], "2026-11-08T19:00:03Z")
        self.assertEqual(v["count"], 50)
        self.assertIn("48 h", v["modo"])

    def test_caso_real_la_ventana_trae_las_horas_de_chile_ya_calculadas(self):
        v = plugin._cu_ventana({"ahora": "2026-11-11T12:00:19Z"})
        self.assertEqual(v["hasta_chile"], "2026-11-11 09:00")
        self.assertEqual(v["desde_chile"], "2026-11-09 09:00")
        self.assertIn("hasta 09:00 CL", v["ventana_para_el_informe"])

    def test_lectura_del_servidor_sin_since_mira_48_horas(self):
        with patch.object(plugin, "_task_user_id", return_value=None):
            _, error, _ = plugin._cu_mensajes_servidor({}, MARTES_19H_UTC)
        self.assertIn("sin token", error)
        self.assertEqual(plugin._CU_SIN_MARCA_HORAS, 48)


class _EntornoCorreo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        self._env.start()
        self.ruta = Path(self._tmp.name) / "work" / "correo_unificado_estado.json"
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        estado = {
            "version": 5,
            "pending": [{"display_id": "#1212", "message_id": MID, "subject": "RE: Consulta licencias"}],
            "resolved": [{"display_id": "#1199", "message_id": "BBB" * 30, "subject": "Viejo"}],
            "discarded": [],
            "next_display_id": 1213,
            "frios_consolidados": {"count": 0},
        }
        self.ruta.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class MailGetAceptaElIdDelListado(_EntornoCorreo):
    def _get(self, mid):
        mensaje = {"id": mid, "subject": "RE: Consulta licencias"}
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_broker_token_check", return_value=("tok", None)),
            patch.object(plugin, "_fetch_message", return_value=(mensaje, None)) as fetch,
        ):
            resultado = json.loads(plugin._get_email_tool({"message_id": mid}))
        return resultado, fetch

    def test_display_id_se_resuelve_al_message_id_del_listado(self):
        for entrada in ("#1212", "1212"):
            resultado, fetch = self._get(entrada)
            self.assertTrue(resultado["success"], entrada)
            self.assertEqual(fetch.call_args.args[1], MID)

    def test_display_id_desconocido_no_llama_a_graph(self):
        resultado, fetch = self._get("#1214")
        self.assertFalse(resultado["success"])
        self.assertIn("#1214", resultado["error"])
        self.assertIn("No inventes", resultado["error"])
        fetch.assert_not_called()

    def test_message_id_real_sigue_funcionando(self):
        resultado, fetch = self._get(MID)
        self.assertTrue(resultado["success"])
        self.assertEqual(fetch.call_args.args[1], MID)


if __name__ == "__main__":
    unittest.main()
