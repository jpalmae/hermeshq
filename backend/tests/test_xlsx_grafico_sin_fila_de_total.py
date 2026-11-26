import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"


class TestGraficoSinTotal(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("xlsx_plugin_sin_total", PLUGIN)
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

    def _rangos(self, filas, tipo="bar"):
        hoja = {
            "name": "P",
            "headers": ["Mes", "Ingresos", "Gastos"],
            "rows": filas,
            "charts": [{"type": tipo, "category_column": "Mes", "title": "G"}],
        }
        r = json.loads(self.plugin._handle_generate_xlsx({"title": "P", "sheets": [hoja]}))
        self.assertTrue(r["success"], r)
        import zipfile

        ruta = Path(self._tmp.name) / r["path"] if not Path(r["path"]).is_absolute() else Path(r["path"])
        z = zipfile.ZipFile(ruta)
        xml = z.read("xl/charts/chart1.xml").decode()
        import re

        return re.findall(r"<(?:c:)?f>([^<]*)</(?:c:)?f>", xml)

    def test_la_fila_total_no_entra_en_el_grafico(self):
        filas = [["Enero", 1000, 700], ["Febrero", 1200, 800], ["Marzo", 900, 950], ["Total", 3100, 2450]]
        rangos = self._rangos(filas)
        self.assertTrue(rangos)
        self.assertTrue(all("$5" not in r for r in rangos), rangos)
        self.assertTrue(any(r.endswith("$4") for r in rangos), rangos)

    def test_sin_fila_total_se_grafica_todo(self):
        filas = [["Enero", 1000, 700], ["Febrero", 1200, 800], ["Marzo", 900, 950]]
        rangos = self._rangos(filas)
        self.assertTrue(any(r.endswith("$4") for r in rangos), rangos)

    def test_una_categoria_que_se_llama_totalidad_si_se_grafica(self):
        filas = [["Enero", 1000, 700], ["Totalidad regional", 1200, 800]]
        rangos = self._rangos(filas)
        self.assertTrue(any(r.endswith("$3") for r in rangos), rangos)
