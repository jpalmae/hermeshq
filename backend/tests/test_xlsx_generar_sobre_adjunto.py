import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_xlsx" / "__init__.py"
ADJ = "[Archivos adjuntos, ya disponibles en tu workspace:\n- matriz.xlsx (13 KB) -> uploads/f46242df-7f29-4a31-85f5-6d2509836737.xlsx]\n"
HOJAS = [{"name": "Hoja", "headers": ["a", "b"], "rows": [[1, 2]]}]


def _cargar():
    spec = importlib.util.spec_from_file_location("xlsx_adjunto", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _generar(mensaje, modulo=None, veces=1):
    modulo = modulo or _cargar()
    salidas = []
    with tempfile.TemporaryDirectory() as tmp:
        entorno = {
            "HERMES_SESSION_CWD": tmp,
            "HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {"mensaje_usuario": mensaje}}),
        }
        with patch.dict(os.environ, entorno):
            for _ in range(veces):
                salidas.append(json.loads(modulo._handle_generate_xlsx({"title": "x", "sheets": HOJAS})))
    return salidas


def test_caso_real_agregar_columnas_a_un_adjunto_se_dirige_a_editar():
    salida = _generar(ADJ + "A la matriz agregale las siguientes columnas: Sucursal, Área")[0]
    assert salida["success"] is False
    assert "hq_xlsx_editar" in salida["error"]
    assert "uploads/f46242df" in salida["error"]


def test_el_aviso_es_una_sola_vez_por_tarea_y_luego_deja_generar():
    primera, segunda = _generar(ADJ + "A la matriz agregale una columna", veces=2)
    assert primera["success"] is False
    assert segunda["success"] is True


def test_pedir_un_archivo_nuevo_a_partir_del_adjunto_no_se_bloquea():
    assert _generar(ADJ + "Con los datos de esta matriz genera un Excel nuevo con los totales por área")[0]["success"]


def test_pedir_un_grafico_a_partir_del_adjunto_no_se_bloquea():
    assert _generar(ADJ + "Agrega un gráfico de barras con estos datos en otro archivo")[0]["success"]


def test_sin_adjunto_no_se_bloquea():
    assert _generar("Genera un Excel con una columna más para el equipo")[0]["success"]


def test_con_adjunto_pero_sin_pedir_cambios_no_se_bloquea():
    assert _generar(ADJ + "Genera un Excel con los datos del trimestre")[0]["success"]


def test_un_adjunto_que_no_es_excel_no_se_bloquea():
    mensaje = "[Archivos adjuntos:\n- doc.docx -> uploads/aaaaaaaa-0000-0000-0000-000000000000.docx]\nAgrega columnas"
    assert _generar(mensaje)[0]["success"]
