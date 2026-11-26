import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_lote"
_spec = importlib.util.spec_from_file_location("hermeshq_lote_copia_test", PLUGIN / "__init__.py")
lote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lote)


class EdicionSinSobrescribirOriginal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve() / "agent-aaa" / "work"
        self.work.mkdir(parents=True)
        self.prev = {k: None for k in ("HERMES_SESSION_CWD",)}
        import os

        self.prev["HERMES_SESSION_CWD"] = os.environ.get("HERMES_SESSION_CWD")
        os.environ["HERMES_SESSION_CWD"] = str(self.work.parent)
        self.addCleanup(self._restaurar_entorno)

    def _restaurar_entorno(self):
        import os

        if self.prev["HERMES_SESSION_CWD"] is None:
            os.environ.pop("HERMES_SESSION_CWD", None)
        else:
            os.environ["HERMES_SESSION_CWD"] = self.prev["HERMES_SESSION_CWD"]

    def _editar(self, ruta, ediciones):
        return json.loads(lote._edit_many_tool({"path": ruta, "edits": ediciones}))

    def test_el_original_no_cambia_y_la_salida_es_una_version_nueva(self):
        original = self.work / "informe.txt"
        original.write_text("Orion uno\nOrion dos\n", encoding="utf-8")
        r = self._editar("work/informe.txt", [{"old": "Orion uno", "new": "DEMO UNO"}])
        self.assertTrue(r["success"], r)
        self.assertEqual(original.read_text(encoding="utf-8"), "Orion uno\nOrion dos\n")
        nueva = self.work / "informe_v2.txt"
        self.assertTrue(nueva.exists(), "debe existir una versión _v2 con el cambio")
        self.assertEqual(nueva.read_text(encoding="utf-8"), "DEMO UNO\nOrion dos\n")
        self.assertEqual(r["archivo_nuevo"], "work/informe_v2.txt")
        self.assertTrue(r["original_intacto"])

    def test_una_segunda_edicion_no_pisa_la_version_anterior(self):
        (self.work / "a.txt").write_text("uno dos", encoding="utf-8")
        self._editar("work/a.txt", [{"old": "uno", "new": "UNO"}])
        r = self._editar("work/a.txt", [{"old": "dos", "new": "DOS"}])
        self.assertTrue(r["success"], r)
        self.assertEqual((self.work / "a_v2.txt").read_text(encoding="utf-8"), "UNO dos")
        self.assertEqual((self.work / "a_v3.txt").read_text(encoding="utf-8"), "uno DOS")

    def test_si_falla_no_se_crea_ninguna_version(self):
        (self.work / "b.txt").write_text("beta", encoding="utf-8")
        r = self._editar("work/b.txt", [{"old": "nada", "new": "x"}])
        self.assertFalse(r["success"])
        self.assertFalse((self.work / "b_v2.txt").exists())
        self.assertEqual((self.work / "b.txt").read_text(encoding="utf-8"), "beta")
