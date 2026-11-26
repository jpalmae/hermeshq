import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_guard_consultas_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class TestConsultasRepetidas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(
            os.environ,
            {
                "HERMES_SESSION_CWD": self.tmp.name,
                "HERMESHQ_SKILL_TEMPLATES": str(RAIZ / "skill_templates"),
                "HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t-1", "metadata": {}}),
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        os.environ.pop("HERMESHQ_GUARDA", None)
        plugin._CONSULTAS_DE_LA_TAREA.clear()

    def _llama(self, tool, args):
        return plugin._guarda_pre_tool_call(tool_name=tool, args=args, task_id="t", session_id="s", tool_call_id="c")

    def test_la_misma_consulta_se_permite_dos_veces_y_se_bloquea_la_tercera(self):
        args = {"folder": "Orden/Proyectos/SpBox (RS)", "count": 30, "since": "2026-11-16T03:00:00Z"}
        self.assertIsNone(self._llama("ms365_mail_list", args))
        self.assertIsNone(self._llama("ms365_mail_list", dict(args)))
        r = self._llama("ms365_mail_list", dict(args))
        self.assertEqual(r["action"], "block")
        self.assertIn("570", r["message"])

    def test_carpetas_distintas_no_cuentan_como_repetidas(self):
        for i in range(8):
            self.assertIsNone(self._llama("ms365_mail_list", {"folder": f"Orden/Proyectos/{i}", "count": 30}))

    def test_tope_por_tarea(self):
        for i in range(plugin.LIMITE_CONSULTAS_CORREO):
            self.assertIsNone(self._llama("ms365_mail_list", {"folder": f"f{i}"}))
        self.assertEqual(self._llama("ms365_mail_list", {"folder": "otra"})["action"], "block")

    def test_otra_tarea_empieza_de_cero(self):
        args = {"folder": "x"}
        for _ in range(2):
            self._llama("ms365_mail_list", args)
        self.assertEqual(self._llama("ms365_mail_list", args)["action"], "block")
        os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps({"task_id": "t-2"})
        self.assertIsNone(self._llama("ms365_mail_list", args))

    def test_otras_herramientas_no_se_vigilan(self):
        for _ in range(10):
            self.assertIsNone(self._llama("read_file", {"path": "work/a.json"}))

    def test_interruptor_off(self):
        os.environ["HERMESHQ_GUARDA"] = "off"
        for _ in range(5):
            self.assertIsNone(self._llama("ms365_mail_list", {"folder": "x"}))


if __name__ == "__main__":
    unittest.main()
