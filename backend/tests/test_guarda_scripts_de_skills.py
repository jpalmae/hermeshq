import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_guard_skills_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

SCRIPT = "/app/workspaces/agent-x/.hermes/skills/usuario/resumen-semanal-equipo/scripts/informe_1pagina.py"


class TestGuardaScriptsDeSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.casa = Path(self.tmp.name) / ".hermes"
        scripts = self.casa / "skills" / "usuario" / "resumen-semanal-equipo" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "informe_1pagina.py").write_text("print(1)\n", encoding="utf-8")
        entorno = patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(self.casa),
                "HERMES_SESSION_CWD": self.tmp.name,
                "HERMESHQ_SKILL_TEMPLATES": str(RAIZ / "skill_templates"),
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        for k in ("HERMESHQ_GUARDA", "HERMESHQ_TASK_PAYLOAD"):
            os.environ.pop(k, None)

    def _tarea(self, **metadata):
        os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps({"metadata": metadata})

    def _llama(self, tool, args):
        return plugin._guarda_pre_tool_call(tool_name=tool, args=args, task_id="t", session_id="s", tool_call_id="c")

    def test_chat_bloquea_parchear_el_script_de_la_skill(self):
        r = self._llama("patch", {"path": SCRIPT, "old_string": "a", "new_string": "b"})
        self.assertEqual(r["action"], "block")

    def test_segundo_plano_bloquea_parchear_el_script_de_la_skill(self):
        self._tarea(background_job=True)
        for tool, args in (
            ("patch", {"path": SCRIPT, "old_string": "a", "new_string": "b"}),
            ("write_file", {"path": SCRIPT, "content": "x"}),
            ("execute_code", {"code": f"open('{SCRIPT}','w').write('x')"}),
        ):
            self.assertEqual(self._llama(tool, args)["action"], "block", tool)

    def test_segundo_plano_bloquea_editar_una_copia_del_script(self):
        self._tarea(background_job=True)
        r = self._llama("write_file", {"path": "work/resumen-semanal-equipo/informe_1pagina.py", "content": "x"})
        self.assertEqual(r["action"], "block")
        self.assertIn("copia de un script de una skill", r["message"])
        r = self._llama(
            "patch", {"path": "work/resumen-semanal-equipo/informe_1pagina.py", "old_string": "a", "new_string": "b"}
        )
        self.assertEqual(r["action"], "block")

    def test_tarea_programada_bloquea_la_copia(self):
        self._tarea(scheduled=True)
        r = self._llama("write_file", {"path": "work/informe_1pagina.py", "content": "x"})
        self.assertEqual(r["action"], "block")

    def test_en_el_chat_la_copia_de_trabajo_se_permite(self):
        self.assertIsNone(self._llama("write_file", {"path": "work/informe_1pagina.py", "content": "x"}))

    def test_segundo_plano_sigue_pudiendo_construir_a_medida(self):
        self._tarea(background_job=True)
        self.assertIsNone(self._llama("write_file", {"path": "work/mi_script.py", "content": "print(1)\n"}))
        self.assertIsNone(self._llama("write_file", {"path": "outputs/app.js", "content": "x"}))
        self.assertIsNone(self._llama("execute_code", {"code": "print('hola')"}))
        self.assertIsNone(self._llama("read_file", {"path": SCRIPT}))

    def test_leer_el_script_de_la_skill_nunca_se_bloquea(self):
        self._tarea(background_job=True)
        self.assertIsNone(self._llama("read_file", {"path": SCRIPT}))
        self.assertIsNone(self._llama("execute_code", {"code": f"print(open('{SCRIPT}').read())"}))

    def test_el_interruptor_off_desactiva(self):
        os.environ["HERMESHQ_GUARDA"] = "off"
        self.assertIsNone(self._llama("patch", {"path": SCRIPT, "old_string": "a", "new_string": "b"}))


if __name__ == "__main__":
    unittest.main()
