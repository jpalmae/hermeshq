import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_comparar_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

HTML_A = """<html><head><title>Informe</title><style>:root{--c1:#112233;--c2:#445566}</style></head><body>
<h1>Gobernanza</h1><p>Resumen del estado. Hay 10 licencias asignadas.</p>
<h2>Riesgos</h2><p>El riesgo principal es el costo. Se revisa cada mes.</p>
<h2>Fuentes</h2><p>Tablero de indicadores.</p><table><tr><td>a</td></tr></table>
<script>var x = "no debe contarse";</script></body></html>"""
HTML_B = """<html><head><title>Informe</title><style>:root{--c1:#aa0000;--c2:#445566}</style></head><body>
<h1>Gobernanza</h1><p>Resumen del estado. Hay 12 licencias asignadas.</p>
<h2>Riesgos y mitigaciones</h2><p>El riesgo principal es el costo. Se revisa cada mes. Se agrega un plan de mitigación.</p>
<h2>Plan de acción</h2><p>Primero ordenar los permisos.</p>
<h2>Fuentes</h2><p>Tablero de indicadores.</p><table><tr><td>a</td></tr><tr><td>b</td></tr></table>
</body></html>"""


def _escribir_csv(ruta, filas):
    with open(ruta, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(filas)


class TestComparar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        (self.raiz / "outputs").mkdir()
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _llamar(self, **args):
        return json.loads(plugin._comparar_versiones(args))

    def test_two_html_reports_show_sections_phrases_style_and_structure(self):
        (self.raiz / "outputs" / "Informe_V2.html").write_text(HTML_A, encoding="utf-8")
        (self.raiz / "outputs" / "Informe_V3.html").write_text(HTML_B, encoding="utf-8")
        r = self._llamar(antes="Informe_V2", despues="Informe_V3")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["tipo"], "html")
        self.assertEqual(r["secciones_nuevas"], ["Plan de acción"])
        self.assertEqual(r["secciones_quitadas"], [])
        modificadas = {m["seccion"]: m for m in r["secciones_modificadas"]}
        self.assertEqual(modificadas["Riesgos y mitigaciones"]["antes_se_llamaba"], "Riesgos")
        self.assertTrue(any("mitigación" in f for f in modificadas["Riesgos y mitigaciones"]["nuevas"]))
        self.assertTrue(any("12 licencias" in f for f in modificadas["Gobernanza"]["nuevas"]))
        self.assertIn("--c1", r["variables_de_estilo_cambiadas"])
        self.assertNotIn("--c2", r["variables_de_estilo_cambiadas"])
        self.assertNotIn("table", r["estructura"])
        self.assertNotIn("no debe contarse", json.dumps(r, ensure_ascii=False))
        self.assertTrue(r["resumen"])

    def test_identical_reports_report_no_changes(self):
        (self.raiz / "outputs" / "a.html").write_text(HTML_A, encoding="utf-8")
        (self.raiz / "outputs" / "b.html").write_text(HTML_A, encoding="utf-8")
        r = self._llamar(antes="a.html", despues="b.html")
        self.assertEqual(r["secciones_modificadas"], [])
        self.assertEqual(r["secciones_nuevas"], [])
        self.assertEqual(r["variables_de_estilo_cambiadas"], {})

    def test_two_csv_files_with_key_show_added_removed_and_changed_rows(self):
        _escribir_csv(
            self.raiz / "uploads_a.csv",
            [
                ["ID", "Usuario", "Licencia", "Costo"],
                [1, "ana", "Pro", 10],
                [2, "luis", "Pro", 10],
                [3, "eva", "Free", 0],
            ],
        )
        _escribir_csv(
            self.raiz / "uploads_b.csv",
            [
                ["ID", "Usuario", "Licencia", "Costo"],
                [1, "ana", "Premium", 20],
                [2, "luis", "Pro", 10],
                [4, "ivo", "Pro", 10],
            ],
        )
        r = self._llamar(antes="uploads_a.csv", despues="uploads_b.csv", clave=["id"])
        self.assertTrue(r["success"], r)
        self.assertEqual((r["filas_agregadas"], r["filas_quitadas"], r["filas_modificadas"]), (1, 1, 1))
        cambios = {c["columna"]: c for c in r["ejemplos_modificadas"][0]["cambios"]}
        self.assertEqual(cambios["Licencia"]["antes"], "Pro")
        self.assertEqual(cambios["Licencia"]["despues"], "Premium")
        self.assertEqual(r["cambios_por_columna"], {"Licencia": 1, "Costo": 1})
        self.assertEqual(r["ejemplos_agregadas"][0]["Usuario"], "ivo")
        self.assertEqual(r["ejemplos_quitadas"][0]["Usuario"], "eva")
        total = {t["columna"]: t for t in r["totales_numericos"]}
        self.assertEqual((total["Costo"]["antes"], total["Costo"]["despues"]), (20.0, 40.0))

    def test_without_key_rows_are_compared_as_a_set_and_new_columns_are_reported(self):
        _escribir_csv(self.raiz / "a.csv", [["Usuario", "Licencia"], ["ana", "Pro"], ["luis", "Pro"]])
        _escribir_csv(
            self.raiz / "b.csv", [["Usuario", "Licencia", "Area"], ["ana", "Pro", "TI"], ["eva", "Pro", "RRHH"]]
        )
        r = self._llamar(antes="a.csv", despues="b.csv")
        self.assertEqual(r["columnas_nuevas"], ["Area"])
        self.assertEqual((r["filas_agregadas"], r["filas_quitadas"]), (1, 1))
        self.assertIn("clave", r["nota"])

    def test_numbers_that_differ_only_in_format_are_not_changes(self):
        _escribir_csv(self.raiz / "a.csv", [["ID", "Costo"], [1, "10,0"], [2, 5], [3, "x"]])
        _escribir_csv(self.raiz / "b.csv", [["ID", "Costo"], [1, 10], [2, "5.0"], [3, "X "]])
        r = self._llamar(antes="a.csv", despues="b.csv", clave=["ID"])
        self.assertEqual(r["filas_modificadas"], 0)

    def test_errors_are_actionable(self):
        self.assertFalse(self._llamar(antes="a.csv")["success"])
        r = self._llamar(antes="no-existe.csv", despues="tampoco.csv")
        self.assertFalse(r["success"])
        self.assertIn("no-existe.csv", r["error"])
        _escribir_csv(self.raiz / "a.csv", [["ID", "Costo"], [1, 2], [2, 3]])
        (self.raiz / "b.html").write_text(HTML_A, encoding="utf-8")
        self.assertIn("mismo tipo", self._llamar(antes="a.csv", despues="b.html")["error"])
        _escribir_csv(self.raiz / "c.csv", [["ID", "Costo"], [1, 2], [2, 3]])
        r = self._llamar(antes="a.csv", despues="c.csv", clave=["Inexistente"])
        self.assertIn("clave", r["error"])

    def test_ambiguous_names_list_candidates(self):
        (self.raiz / "outputs" / "Informe_V2.html").write_text(HTML_A, encoding="utf-8")
        (self.raiz / "outputs" / "Informe_V2 1.html").write_text(HTML_B, encoding="utf-8")
        (self.raiz / "outputs" / "otro.html").write_text(HTML_A, encoding="utf-8")
        r = self._llamar(antes="Informe_V", despues="otro")
        self.assertFalse(r["success"])
        self.assertEqual(len(r["candidatos"]), 2)
        exacto = self._llamar(antes="Informe_V2.html", despues="otro")
        self.assertTrue(exacto["success"], exacto)


if __name__ == "__main__":
    unittest.main()
