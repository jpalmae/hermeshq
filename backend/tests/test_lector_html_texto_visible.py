import importlib.util
from pathlib import Path

from hermeshq.services.archivos_conversacion import linea_archivos

_RUTA = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "lector.py"
_spec = importlib.util.spec_from_file_location("lector_html_texto", _RUTA)
lector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lector)

HTML = (
    "<!DOCTYPE html><html><head><title>T</title><style>.a{color:red;}</style></head><body>"
    "<script>var DATOS='x'.repeat(5000);</script><h1>Levantamiento</h1><section><h2>Metodología</h2>"
    "<p>Se entrevista a las sucursales.</p><table><tr><th>Proceso</th><th>Prioridad</th></tr>"
    "<tr><td>Despacho</td><td>Alta</td></tr></table></section></body></html>"
)


def test_el_html_se_lee_como_texto_visible_sin_css_ni_scripts(tmp_path):
    archivo = tmp_path / "informe.html"
    archivo.write_text(HTML, encoding="utf-8")
    salida = lector.leer(archivo, max_caracteres=10000)
    assert salida["success"] and salida["tipo"] == "html"
    texto = salida["texto"]
    assert "# Levantamiento" in texto and "## Metodología" in texto
    assert "Despacho | Alta" in texto or "Despacho | Alta |" in texto
    assert "color:red" not in texto and "DATOS" not in texto and "<h1>" not in texto
    assert salida["caracteres"] < 300


def test_control_negativo_un_txt_sigue_leyendose_tal_cual(tmp_path):
    archivo = tmp_path / "nota.txt"
    archivo.write_text("<b>hola</b>", encoding="utf-8")
    assert lector.leer(archivo, max_caracteres=100)["texto"] == "<b>hola</b>"


def test_la_nota_de_archivos_generados_manda_a_hq_leer_documento():
    nota = linea_archivos([{"ruta": "uploads/a.html", "task_id": "28eda3a7-0000", "nombre": "Informe.html"}])
    assert "hq_leer_documento" in nota and "read_file" in nota
