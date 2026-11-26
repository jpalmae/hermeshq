import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_contenido_fiel", _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

PEDIDO_ANA = (
    "Hace un informe interactivo de 1 sola pagina explicando lo que sale en la imagen con titulo Estructura Report "
    "Server. Además, agregar dos árbol jerárquico uno de Empresa (Unidad A) y otro Filial Sur (Unidad B), en "
    "Empresa habrían carpetas de SUC1, SUC2, SUC3,SUC4,SUC5,SUC6 y en Filial Sur SUC1,SUC7, SUC8, SUC9 y SUC10. GUardalo en "
    "C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Proyectos\\Proyecto Demo\\PROYECTO DEMO\\Etapa 2 - "
    "Estructura RS y el archivo que se llame Estructura Demo"
)
VISION_REAL = {
    "success": True,
    "analysis": (
        "The user wants me to describe the image. The question mentions folders that are NOT in this image: "
        '"Proyecto Demo", "PROYECTO DEMO", "Etapa 2 - Estructura", "Carpeta 00", "Carpeta 01", "Carpeta 02", etc. '
        'These folders are NOT visible in the image. The image shows "Modelo de Accesos (Demo)".'
    ),
}
ARBOLES_COMPLETOS = """# Estructura
intro: Estructura propuesta.
fuentes: f1
arbol: Empresa (Unidad A)
- Empresa
  - SUC1
  - SUC2
  - SUC3
  - SUC4
  - SUC5
  - SUC6

arbol: Filial Sur (Unidad B)
- Filial Sur
  - SUC1
  - SUC7
  - SUC8
  - SUC9
  - SUC10
"""


def _mensajes_con_vision(resultado):
    return [
        {"role": "user", "content": "describe"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "v1", "function": {"name": "vision_analyze"}}]},
        {"role": "tool", "tool_call_id": "v1", "content": json.dumps(resultado)},
    ]


class TestNegadosPorVision(unittest.TestCase):
    def test_caso_real_extrae_lo_que_la_vision_niega(self):
        negados = runner.negados_por_vision(_mensajes_con_vision(VISION_REAL))
        for item in ("Proyecto Demo", "PROYECTO DEMO", "Etapa 2 - Estructura", "Carpeta 00"):
            self.assertIn(item, negados)
        self.assertNotIn("Modelo de Accesos (Demo)", negados)

    def test_otras_herramientas_y_afirmaciones_no_cuentan(self):
        mensajes = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "x", "content": 'NOT in this image: "Ventas"'},
        ]
        self.assertEqual(runner.negados_por_vision(mensajes), [])
        afirmado = {"analysis": 'The image shows "Report Server" and "DEMO".'}
        self.assertEqual(runner.negados_por_vision(_mensajes_con_vision(afirmado)), [])


class _EnTarea(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _crear(self, capitulos_texto, negados=None, pedido=PEDIDO_ANA):
        if negados is not None:
            (self.base / ".hermes").mkdir(exist_ok=True)
            (self.base / ".hermes" / "vision_negados.json").write_text(
                json.dumps({"task_id": "t1", "negados": negados}), encoding="utf-8"
            )
        spec = {
            "titulo": "Estructura Demo",
            "area": "Proyecto Demo",
            "fecha": "Noviembre 2026",
            "objetivo": "Explicar la imagen.",
            "resumen": {"conclusion": "Prueba."},
            "fuentes": [{"id": "f1", "nombre": "Imagen", "tipo": "Imagen", "uso": "Estructura"}],
            "capitulos_texto": capitulos_texto,
        }
        payload = json.dumps({"task_id": "t1", "metadata": {"mensaje_usuario": pedido}})
        with patch.dict(
            os.environ,
            {
                "HERMESHQ_SKILL_TEMPLATES": str(_RAIZ / "skill_templates"),
                "HERMES_SESSION_CWD": str(self.base),
                "HERMESHQ_TASK_PAYLOAD": payload,
            },
        ):
            return json.loads(plugin._crear_informe({"spec": spec}))


class TestSinParrafosEnIngles(unittest.TestCase):
    def test_caso_real_preambulo_en_ingles(self):
        texto = (
            "I'll analyze this Excel file to detect reprocesos. Let me start by loading the data from both sheets.\n\n"
            "---\n\n## Análisis de Reprocesos\n\nSe detectaron 3 reprocesos en el cruce de las hojas."
        )
        limpio = runner.sin_parrafos_en_ingles(texto)
        self.assertTrue(limpio.startswith("---"))
        self.assertNotIn("I'll analyze", limpio)

    def test_respuesta_en_espanol_o_entera_en_ingles_no_se_toca(self):
        es = "Listo. El informe quedó en la carpeta de Pruebas.\n\nIncluye dos árboles."
        self.assertEqual(runner.sin_parrafos_en_ingles(es), es)
        en = "This is the report you asked for.\n\nIt has two trees and the folder list."
        self.assertEqual(runner.sin_parrafos_en_ingles(en), en)

    def test_tablas_y_nombres_en_ingles_se_conservan(self):
        texto = "Resumen de la carpeta.\n\n| Data Sources | Datasets |\n|---|---|\n| 3 | 5 |"
        self.assertEqual(runner.sin_parrafos_en_ingles(texto), texto)


class TestRespuestaEnIngles(unittest.TestCase):
    REAL = (
        "I don't have a tool to collect security alerts. Let me check what's available.\n\n"
        "I have no tool or integration that connects to a security monitoring system, so I can't collect the alerts "
        "from the last 30 minutes for you."
    )

    def test_caso_real_cron_de_alertas_en_ingles(self):
        self.assertTrue(
            runner.respuesta_en_ingles(self.REAL, "Recopila las alertas de seguridad de los últimos 30 minutos")
        )

    def test_respuesta_en_espanol_con_nombres_en_ingles_no_cuenta(self):
        es = (
            "Listo. Revisé el informe de Data Sources y Datasets: hay 3 orígenes y 5 conjuntos de datos en el servidor."
        )
        self.assertFalse(runner.respuesta_en_ingles(es, "revisa el informe"))

    def test_usuario_que_escribe_en_ingles_no_se_corrige(self):
        self.assertFalse(runner.respuesta_en_ingles(self.REAL, "Collect the security alerts from the last 30 minutes"))

    def test_respuesta_corta_no_cuenta(self):
        self.assertFalse(runner.respuesta_en_ingles("OK, done.", "hazlo"))
