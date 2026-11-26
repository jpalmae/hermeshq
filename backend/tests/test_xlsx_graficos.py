import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"

VENTAS = {
    "name": "Ventas",
    "headers": ["Mes", "Norte", "Centro", "Sur", "Total"],
    "rows": [
        ["Enero", 120000, 95000, 78000, 293000],
        ["Febrero", 135000, 102000, 84000, 321000],
        ["Marzo", 148000, 110000, 91000, 349000],
    ],
}


class TestGraficosExcel(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("xlsx_plugin_graficos", PLUGIN)
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

    def _generar(self, hoja):
        return json.loads(self.plugin._handle_generate_xlsx({"title": "Ventas H1", "sheets": [hoja]}))

    def _graficos(self, ruta):
        from openpyxl import load_workbook

        return load_workbook(ruta)["Ventas"]._charts

    def test_grafico_de_lineas_con_series_por_nombre(self):
        hoja = dict(VENTAS, charts=[{"type": "line", "title": "Ventas", "series": ["Norte", "Centro", "Sur"]}])
        resultado = self._generar(hoja)
        self.assertTrue(resultado["success"], resultado)
        self.assertEqual(
            resultado["graficos"], [{"hoja": "Ventas", "tipo": "line", "series": ["Norte", "Centro", "Sur"]}]
        )
        graficos = self._graficos(resultado["path"])
        self.assertEqual(len(graficos), 1)
        self.assertEqual(type(graficos[0]).__name__, "LineChart")
        self.assertEqual(len(graficos[0].series), 3)

    def test_series_por_defecto_son_las_columnas_numericas(self):
        resultado = self._generar(dict(VENTAS, charts=[{"type": "column"}]))
        self.assertTrue(resultado["success"], resultado)
        self.assertEqual(resultado["graficos"][0]["series"], ["Norte", "Centro", "Sur", "Total"])
        self.assertEqual(type(self._graficos(resultado["path"])[0]).__name__, "BarChart")

    def test_serie_inexistente_falla_sin_archivo(self):
        resultado = self._generar(dict(VENTAS, charts=[{"type": "line", "series": ["Oeste"]}]))
        self.assertFalse(resultado["success"])
        self.assertIn("Oeste", resultado["error"])
        self.assertFalse(list((Path(self._tmp.name) / "output").glob("*.xlsx")))

    def test_sin_graficos_no_agrega_ninguno(self):
        resultado = self._generar(VENTAS)
        self.assertTrue(resultado["success"])
        self.assertEqual(resultado["graficos"], [])
        self.assertEqual(self._graficos(resultado["path"]), [])


SUCURSALES = {
    "name": "Ventas",
    "headers": ["Sucursal", "Enero", "Febrero", "Marzo", "Abril", "Total"],
    "rows": [
        ["Ñuñoa", 12000000, 13500000, 14200000, 15000000, 54700000],
        ["Peñalolén", 8500000, 9200000, 9800000, 10500000, 38000000],
        ["Concepción", 6800000, 7100000, 7500000, 7900000, 29300000],
        ["Total", 27300000, 29800000, 31500000, 33400000, 122000000],
    ],
}


class TestGraficosPorFilas(TestGraficosExcel):
    def _hoja(self, ruta):
        from openpyxl import load_workbook

        return load_workbook(ruta)["Ventas"]

    def _comprobar_evolucion_por_sucursal(self, resultado):
        self.assertTrue(resultado["success"], resultado)
        self.assertEqual(
            resultado["graficos"],
            [{"hoja": "Ventas", "tipo": "line", "series": ["Ñuñoa", "Peñalolén", "Concepción"], "series_por": "filas"}],
        )
        grafico = self._hoja(resultado["path"])._charts[0]
        self.assertEqual(len(grafico.series), 3)
        refs = [s.val.numRef.f for s in grafico.series]
        self.assertEqual(refs, ["'Ventas'!$B$2:$E$2", "'Ventas'!$B$3:$E$3", "'Ventas'!$B$4:$E$4"])
        self.assertEqual(
            grafico.series[0].cat.numRef.f if grafico.series[0].cat.numRef else grafico.series[0].cat.strRef.f,
            "'Ventas'!$B$1:$E$1",
        )
        self.assertEqual([s.tx.v for s in grafico.series], ["Ñuñoa", "Peñalolén", "Concepción"])

    def test_series_from_rows_grafica_cada_sucursal_a_lo_largo_de_los_meses(self):
        hoja = dict(SUCURSALES, charts=[{"type": "line", "title": "Evolución", "series_from_rows": True}])
        self._comprobar_evolucion_por_sucursal(self._generar(hoja))

    def test_nombres_de_fila_en_series_activan_las_filas_solos(self):
        hoja = dict(SUCURSALES, charts=[{"type": "line", "series": ["Ñuñoa", "peñalolén", "Concepción"]}])
        resultado = self._generar(hoja)
        self.assertTrue(resultado["success"], resultado)
        self.assertEqual(resultado["graficos"][0]["series_por"], "filas")
        self.assertEqual(resultado["graficos"][0]["series"], ["Ñuñoa", "Peñalolén", "Concepción"])

    def test_sin_la_opcion_sigue_siendo_por_columnas(self):
        resultado = self._generar(dict(SUCURSALES, charts=[{"type": "line"}]))
        self.assertTrue(resultado["success"], resultado)
        self.assertNotIn("series_por", resultado["graficos"][0])
        self.assertEqual(resultado["graficos"][0]["series"], ["Enero", "Febrero", "Marzo", "Abril", "Total"])

    def test_caso_real_category_column_sin_sentido_usa_las_etiquetas_y_lo_avisa(self):
        hoja = dict(
            SUCURSALES,
            charts=[{"type": "line", "category_column": "Uncategorized Column", "series_from_rows": True}],
        )
        resultado = self._generar(hoja)
        self.assertTrue(resultado["success"], resultado)
        grafico = resultado["graficos"][0]
        self.assertEqual(grafico["series"], ["Ñuñoa", "Peñalolén", "Concepción"])
        self.assertIn("Uncategorized Column", grafico["aviso"])
        self.assertIn("Sucursal", grafico["aviso"])

    def test_category_column_invalida_sin_etiquetas_de_texto_sigue_fallando(self):
        hoja = {
            "name": "Ventas",
            "headers": ["A", "B"],
            "rows": [[1, 2], [3, 4]],
            "charts": [{"type": "line", "category_column": "Nada"}],
        }
        resultado = self._generar(hoja)
        self.assertFalse(resultado["success"])

    def test_fila_inexistente_falla_sin_archivo(self):
        hoja = dict(SUCURSALES, charts=[{"type": "line", "series_from_rows": True, "series": ["Valdivia"]}])
        resultado = self._generar(hoja)
        self.assertFalse(resultado["success"])
        self.assertIn("Valdivia", resultado["error"])
        self.assertFalse(list((Path(self._tmp.name) / "output").glob("*.xlsx")))

    def test_columnas_no_contiguas_se_rechazan_con_un_mensaje_claro(self):
        hoja = {
            "name": "Ventas",
            "headers": ["Sucursal", "Enero", "Nota", "Febrero"],
            "rows": [["Ñuñoa", 1, "x", 2], ["Peñalolén", 3, "y", 4]],
            "charts": [{"type": "line", "series_from_rows": True}],
        }
        resultado = self._generar(hoja)
        self.assertFalse(resultado["success"])
        self.assertIn("contiguas", resultado["error"])


if __name__ == "__main__":
    unittest.main()
