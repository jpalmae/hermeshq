import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"
_spec = importlib.util.spec_from_file_location("xlsx_plugin_fila_total", PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

PEDIDO = "Hazme un Excel con meses, una columna Resultado y una fila Total con fórmulas"
HOJA_SIN = [{"name": "P", "headers": ["Mes", "I"], "rows": [["Enero", 1], ["Febrero", 2]]}]
HOJA_CON = [{"name": "P", "headers": ["Mes", "I"], "rows": [["Enero", 1], ["Febrero", 2], ["Total", "=SUM(B2:B3)"]]}]


def _env(mensaje):
    return {"HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {"mensaje_usuario": mensaje}})}


def test_pide_total_y_no_hay_se_rechaza():
    with TemporaryDirectory() as tmp, patch.dict(os.environ, {**_env(PEDIDO), "HERMES_SESSION_CWD": tmp}):
        salida = json.loads(plugin._handle_generate_xlsx({"title": "P", "sheets": HOJA_SIN}))
    assert salida["success"] is False and "fila Total" in salida["error"]


def test_pide_total_y_hay_se_genera():
    with TemporaryDirectory() as tmp, patch.dict(os.environ, {**_env(PEDIDO), "HERMES_SESSION_CWD": tmp}):
        salida = json.loads(plugin._handle_generate_xlsx({"title": "P", "sheets": HOJA_CON}))
    assert salida["success"] is True


def test_sin_pedido_de_total_no_se_exige():
    with (
        TemporaryDirectory() as tmp,
        patch.dict(os.environ, {**_env("Hazme un Excel con meses y gastos"), "HERMES_SESSION_CWD": tmp}),
    ):
        salida = json.loads(plugin._handle_generate_xlsx({"title": "P", "sheets": HOJA_SIN}))
    assert salida["success"] is True


def test_totales_en_plural_cuenta():
    hoja = [{"name": "P", "headers": ["Mes", "I"], "rows": [["Enero", 1], ["Totales", 1]]}]
    with patch.dict(os.environ, _env(PEDIDO)):
        assert plugin._omite_la_fila_total(hoja) is False
