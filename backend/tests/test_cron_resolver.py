import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermeshq_schedule_resolver", PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

CRON = "020c565d-c5fe-4346-868c-3e37d53bca58"
CORRIDA = "3c35b9b3-32e8-4106-858b-ac53be2e5104"
LISTA = {
    "success": True,
    "schedules": [
        {"id": CRON, "name": "Resumen semanal de correos no leídos"},
        {"id": "otro-cron-0002", "name": "Informe diario"},
    ],
}


class TestResolverCron(unittest.TestCase):
    def setUp(self):
        self.llamadas = []

        def api(method, path, payload=None):
            self.llamadas.append((method, path))
            if method == "GET" and path.startswith("/agents/self/schedules?"):
                return json.dumps(LISTA)
            if not any(c["id"] in path for c in LISTA["schedules"]):
                return json.dumps({"success": False, "error": "Schedule not found"})
            return json.dumps({"success": True, "run_task_id": "nueva"})

        env = patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t1", "metadata": {}})})
        env.start()
        self.addCleanup(env.stop)
        parche = patch.object(plugin, "_api_request", side_effect=api)
        parche.start()
        self.addCleanup(parche.stop)

    def _acciones(self):
        return [
            p
            for m, p in self.llamadas
            if not p.startswith("/agents/self/schedules?") and any(c["id"] in p for c in LISTA["schedules"])
        ]

    def test_caso_real_el_id_de_una_corrida_no_ejecuta_nada_y_lista_los_crons(self):
        out = json.loads(plugin.hq_schedule_runs_tool({"schedule_id": CORRIDA}))
        self.assertFalse(out["success"])
        self.assertIn("Resumen semanal de correos no leídos", out["error"])
        self.assertIn(CRON, out["error"])
        self.assertEqual(self._acciones(), [])
        out = json.loads(plugin.hq_schedule_run_tool({"schedule_id": CORRIDA}))
        self.assertFalse(out["success"])
        self.assertEqual(self._acciones(), [])

    def test_por_nombre_sin_tildes_ni_mayusculas(self):
        out = json.loads(plugin.hq_schedule_run_tool({"schedule_id": "resumen semanal de correos no leidos"}))
        self.assertTrue(out["success"], out)
        self.assertEqual(self._acciones(), [f"/agents/self/schedules/{CRON}/run"])

    def test_el_id_real_sigue_igual(self):
        plugin.hq_schedule_pause_tool({"schedule_id": CRON})
        plugin.hq_schedule_delete_tool({"schedule_id": CRON})
        self.assertEqual(
            self._acciones(),
            [f"/agents/self/schedules/{CRON}/pause", f"/agents/self/schedules/{CRON}?task_id=t1"],
        )

    def test_nombre_parcial_no_adivina(self):
        out = json.loads(plugin.hq_schedule_resume_tool({"schedule_id": "Resumen"}))
        self.assertFalse(out["success"])
        self.assertEqual(self._acciones(), [])


if __name__ == "__main__":
    unittest.main()
