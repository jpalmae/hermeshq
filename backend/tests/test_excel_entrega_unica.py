import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hermeshq.services.hermes_runtime import ultimas_versiones

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"

HOJAS = [
    {
        "name": "Reprocesos",
        "headers": ["Equipo", "OT base", "OT posterior", "Días"],
        "rows": [["EQ-0049", 100000001, 200027193, 2], ["EQ-0049", 100000002, 300045267, 15]],
    },
    {"name": "Estadisticas", "headers": ["Métrica", "Valor"], "rows": [["Total OT", 3315], ["Reprocesos", 2]]},
]


class TestUltimasVersiones(unittest.TestCase):
    def test_caso_real_cinco_intentos_entregan_solo_el_ultimo(self):
        rutas = [f"/ws/output/Reprocesos_Mantencion{'' if n == 1 else f'-{n}'}.xlsx" for n in range(1, 6)]
        self.assertEqual(ultimas_versiones(rutas), ["/ws/output/Reprocesos_Mantencion-5.xlsx"])

    def test_archivos_distintos_se_entregan_todos(self):
        rutas = ["/ws/output/Ventas.xlsx", "/ws/output/Resumen.pdf", "/ws/output/Ventas.pdf", "/ws/work/Ventas.xlsx"]
        self.assertEqual(ultimas_versiones(rutas), rutas)

    def test_años_en_el_nombre_no_son_versiones(self):
        rutas = ["/ws/output/Ventas-2025.xlsx", "/ws/output/Ventas-2026.xlsx", "/ws/output/Informe-Q3.xlsx"]
        self.assertEqual(ultimas_versiones(rutas), rutas)

    def test_repetidos_y_orden(self):
        rutas = ["/o/A.xlsx", "/o/B.xlsx", "/o/A-2.xlsx", "/o/A-2.xlsx"]
        self.assertEqual(ultimas_versiones(rutas), ["/o/B.xlsx", "/o/A-2.xlsx"])


class TestHojasDesdeArchivo(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("xlsx_plugin_entrega", PLUGIN)
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        previo = os.environ.get("HERMES_SESSION_CWD")
        os.environ["HERMES_SESSION_CWD"] = self._tmp.name
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("HERMES_SESSION_CWD", previo)
                if previo
                else os.environ.pop("HERMES_SESSION_CWD", None)
            )
        )
        self.base = Path(self._tmp.name)

    def _generar(self, **args):
        return json.loads(self.plugin._handle_generate_xlsx({"title": "Reprocesos", **args}))

    def test_sheets_file_genera_todas_las_hojas_en_una_llamada(self):
        (self.base / "work" / "reprocesos").mkdir(parents=True)
        (self.base / "work" / "reprocesos" / "hojas.json").write_text(json.dumps(HOJAS), encoding="utf-8")
        res = self._generar(sheets_file="work/reprocesos/hojas.json", filename="Reprocesos_Mantencion.xlsx")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["filas_por_hoja"], {"Reprocesos": 2, "Estadisticas": 2})
        self.assertNotIn("aviso", res)
        from openpyxl import load_workbook

        libro = load_workbook(res["path"])
        self.assertEqual(libro.sheetnames, ["Reprocesos", "Estadisticas"])
        self.assertEqual(libro["Reprocesos"]["A2"].value, "EQ-0049")

    def test_sheets_file_con_clave_sheets(self):
        (self.base / "hojas.json").write_text(json.dumps({"sheets": HOJAS}), encoding="utf-8")
        self.assertTrue(self._generar(sheets_file="hojas.json")["success"])

    def test_sheets_file_fuera_del_espacio_se_rechaza(self):
        res = self._generar(sheets_file="/etc/passwd")
        self.assertFalse(res["success"])
        self.assertIn("fuera del espacio", res["error"])

    def test_sheets_file_inexistente_o_invalido(self):
        self.assertIn("No encuentro", self._generar(sheets_file="work/nada.json")["error"])
        (self.base / "malo.json").write_text("{no es json", encoding="utf-8")
        self.assertIn("JSON válido", self._generar(sheets_file="malo.json")["error"])

    def test_caso_real_hojas_sin_filas_avisan(self):
        vacias = [{"name": h["name"], "headers": h["headers"], "rows": []} for h in HOJAS]
        res = self._generar(sheets=vacias)
        self.assertTrue(res["success"])
        self.assertEqual(res["filas_por_hoja"], {"Reprocesos": 0, "Estadisticas": 0})
        self.assertIn("Reprocesos, Estadisticas", res["aviso"])

    def test_sin_sheets_ni_sheets_file_sigue_fallando(self):
        self.assertFalse(self._generar()["success"])


class TestVersionesDeLosEditores:
    def test_pptx_v2_y_v3_se_entrega_solo_la_ultima(self):
        from hermeshq.services.hermes_runtime import ultimas_versiones

        rutas = ["outputs/Gobernanza_V4_v2.pptx", "outputs/Gobernanza_V4_v3.pptx"]
        assert ultimas_versiones(rutas) == ["outputs/Gobernanza_V4_v3.pptx"]

    def test_encadenadas_y_editado(self):
        from hermeshq.services.hermes_runtime import ultimas_versiones

        assert ultimas_versiones(["a/X.docx", "a/X_editado.docx", "a/X_editado-2.docx"]) == ["a/X_editado-2.docx"]
        assert ultimas_versiones(["uploads/b_v2.pptx", "uploads/b_v2_v2.pptx"]) == ["uploads/b_v2_v2.pptx"]

    def test_control_negativo_archivos_distintos_se_conservan(self):
        from hermeshq.services.hermes_runtime import ultimas_versiones

        rutas = ["outputs/Informe_Ventas.pptx", "outputs/Informe_Costos_v2.pptx", "outputs/Informe_Ventas.docx"]
        assert ultimas_versiones(rutas) == rutas
