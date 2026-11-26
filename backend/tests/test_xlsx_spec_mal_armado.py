import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"
_spec = importlib.util.spec_from_file_location("xlsx_plugin_spec_mal_armado", PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def _generar(args):
    with (
        TemporaryDirectory() as tmp,
        patch.dict(os.environ, {"HERMES_SESSION_CWD": tmp, "HERMESHQ_TASK_PAYLOAD": "{}"}),
    ):
        return json.loads(plugin._handle_generate_xlsx(args)), list(Path(tmp).glob("output/*.xlsx"))


def test_una_hoja_que_no_es_objeto_se_rechaza_y_no_deja_archivo():
    salida, archivos = _generar({"title": "P", "sheets": ['[{"name": "Hoja1", "rows": [["a", "b"]}]']})
    assert salida["success"] is False and "no es un objeto" in salida["error"]
    assert archivos == []


def test_una_fila_que_no_es_lista_se_rechaza():
    salida, archivos = _generar({"title": "P", "sheets": [{"name": "H", "headers": ["A"], "rows": [["x"], "y"]}]})
    assert salida["success"] is False and "fila 2" in salida["error"]
    assert archivos == []


def test_control_negativo_un_spec_bien_armado_se_genera_con_sus_filas():
    salida, archivos = _generar({"title": "P", "sheets": [{"name": "H", "headers": ["A"], "rows": [["x"], ["y"]]}]})
    assert salida["success"] is True and salida["filas_por_hoja"] == {"H": 2}
    assert len(archivos) == 1
