import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_guard_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

HTML_LARGO = "<html><head><style>body{margin:0}</style></head><body>" + ("<p>fila</p>" * 900) + "</body></html>"


class TestGuarda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(
            os.environ, {"HERMESHQ_SKILL_TEMPLATES": str(RAIZ / "skill_templates"), "HERMES_SESSION_CWD": self.tmp.name}
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        os.environ.pop("HERMESHQ_GUARDA", None)
        self.raiz = Path(self.tmp.name)

    def _bloquea(self, tool, args):
        return plugin._guarda_pre_tool_call(tool_name=tool, args=args, task_id="t", session_id="s", tool_call_id="c")

    def test_small_html_and_unrelated_files_are_allowed(self):
        self.assertIsNone(
            self._bloquea("write_file", {"path": "work/nota.html", "content": "<html><p>hola</p></html>"})
        )
        self.assertIsNone(self._bloquea("write_file", {"path": "work/estado.json", "content": "x" * 20000}))
        self.assertIsNone(self._bloquea("write_file", {"path": "work/calculo.py", "content": "print(1)\n" * 2000}))
        self.assertIsNone(self._bloquea("execute_code", {"code": "print('hola')"}))
        self.assertIsNone(self._bloquea("read_file", {"path": "outputs/x.html"}))
