import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    spec = importlib.util.spec_from_file_location("hq_xlsx_num", RAIZ / "hermeshq_xlsx" / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_numeros_escritos_como_texto_se_guardan_como_numeros_y_se_pueden_sumar(plugin):
    openpyxl = pytest.importorskip("openpyxl")
    r = json.loads(
        plugin._handle_generate_xlsx(
            {
                "title": "T",
                "sheets": [
                    {
                        "name": "Datos",
                        "headers": ["Zona", "Ventas", "Codigo", "Tasa"],
                        "rows": [["Norte", "120", "007", "0.5"], ["Sur", "95", "A1", "3"]],
                    }
                ],
            }
        )
    )
    assert r["success"] is True
    ws = openpyxl.load_workbook(r["path"])["Datos"]
    assert ws["B2"].value == 120 and ws["B3"].value == 95
    assert ws["D2"].value == 0.5 and ws["D3"].value == 3
    assert ws["C2"].value == "007" and ws["C3"].value == "A1" and ws["A2"].value == "Norte"
    assert ws["B2"].data_type == "n"


def test_la_suma_entre_hojas_y_el_grafico_ven_numeros(plugin):
    openpyxl = pytest.importorskip("openpyxl")
    r = json.loads(
        plugin._handle_generate_xlsx(
            {
                "title": "T",
                "sheets": [
                    {"name": "Datos", "headers": ["Zona", "Ventas"], "rows": [["N", "120"], ["S", "95"]]},
                    {
                        "name": "Resumen",
                        "headers": ["Concepto", "Valor"],
                        "rows": [["Total", "=SUM(Datos!B2:B3)"]],
                        "charts": [
                            {
                                "type": "bar",
                                "title": "V",
                                "data_sheet": "Datos",
                                "category_column": "Zona",
                                "series": ["Ventas"],
                            }
                        ],
                    },
                ],
            }
        )
    )
    assert r["success"] is True
    libro = openpyxl.load_workbook(r["path"])
    assert libro["Datos"]["B2"].data_type == "n"
    assert libro["Resumen"]["B2"].value == "=SUM(Datos!B2:B3)"
