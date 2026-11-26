import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

RUTA = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"


def _plugin():
    spec = importlib.util.spec_from_file_location("schedule_plugin_trabajo_largo", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _llamar(instrucciones, mensaje_usuario):
    plugin = _plugin()
    enviado = {}

    def fake_api(metodo, ruta, cuerpo=None, *a, **k):
        enviado.update(cuerpo or {})
        return json.dumps({"success": True})

    payload = {"task_id": "t1", "metadata": {"mensaje_usuario": mensaje_usuario}}
    with (
        patch.object(plugin, "_task_payload", return_value=payload),
        patch.object(plugin, "_task_metadata", return_value={}),
        patch.object(plugin, "_api_request", side_effect=fake_api),
    ):
        plugin.hq_trabajo_largo_tool({"titulo": "Página", "instrucciones": instrucciones})
    return enviado["instructions"]


def test_solo_adjuntar_prohibe_subir_a_onedrive():
    texto = _llamar(
        "Crea un HTML y guárdalo en OneDrive en UltraBot-archivos/.", "Hazme la página. Solo adjunta el archivo."
    )
    assert "NO lo subas" in texto and "aunque estas instrucciones lo mencionen" in texto


def test_control_negativo_sin_html_ni_restriccion_no_cambia_nada():
    original = "Analiza las ventas de octubre y resume por zona."
    assert _llamar(original, "Analiza las ventas") == original


def test_control_negativo_el_limite_de_8000_caracteres_se_respeta():
    original = "Crea un HTML. " + "x" * 7900
    assert len(_llamar(original, "Hazme la página. Solo adjunta el archivo.")) <= 8000
