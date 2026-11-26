import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_hora_local", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

CASO_REAL = {"id": "m1", "subject": "Prueba despliegue 10-11", "receivedDateTime": "2026-11-11T01:06:51Z"}


class TestHoraChile(unittest.TestCase):
    def test_caso_real_un_correo_de_anoche_no_es_de_hoy(self):
        self.assertEqual(plugin._hora_chile("2026-11-11T01:06:51Z"), "martes 10-11-2026 22:06")

    def test_horario_de_invierno_y_datos_raros(self):
        self.assertEqual(plugin._hora_chile("2026-07-01T14:00:00Z"), "miércoles 01-07-2026 10:00")
        self.assertIsNone(plugin._hora_chile("no es fecha"))
        self.assertIsNone(plugin._hora_chile(None))


class TestListadoYBusqueda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _con(self):
        return (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_token_herramienta", return_value=("tok", "")),
            patch.object(plugin, "_broker_token_check", return_value=("tok", "")),
            patch.object(plugin, "_broker_graph", return_value={"value": [dict(CASO_REAL)]}),
        )

    def test_el_listado_trae_la_fecha_local_y_la_hora_actual(self):
        a, b, c, d = self._con()
        with a, b, c, d:
            out = json.loads(plugin._list_emails_tool({"count": 5}))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["messages"][0]["recibido_chile"], "martes 10-11-2026 22:06")
        self.assertEqual(out["messages"][0]["receivedDateTime"], "2026-11-11T01:06:51Z")
        self.assertRegex(out["ahora_chile"], r"^\w+ \d{2}-\d{2}-\d{4} \d{2}:\d{2}$")

    def test_mensajes_sin_fecha_no_rompen(self):
        mensajes = [{"id": "x"}, "texto", {"id": "y", "sentDateTime": "2026-11-11T12:00:00Z"}]
        plugin._con_hora_local(mensajes)
        self.assertNotIn("recibido_chile", mensajes[0])
        self.assertEqual(mensajes[2]["enviado_chile"], "miércoles 11-11-2026 09:00")


if __name__ == "__main__":
    unittest.main()
