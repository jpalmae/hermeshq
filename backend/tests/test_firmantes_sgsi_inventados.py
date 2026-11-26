import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
spec = importlib.util.spec_from_file_location("hermeshq_documentos_firmantes", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

PEDIDO_REAL = (
    "crea un archivo Word llamado Validacion lote 21 con el texto «prueba de carpeta pedida» y guárdalo en "
    "C:\\Users\\usuario\\OneDrive - Empresa\\Pruebas HermesHQ\\Lote 21"
)
SPEC_REAL = {
    "titulo": "Validacion lote 21",
    "abreviacion": "VAL-L21",
    "numero": "001",
    "clasificacion": "Interno",
    "fecha_aprobacion": "2026-11-12",
    "elaborado": ["Juan Pérez – Analista HSE"],
    "validado": ["Carla Ruiz – Jefa de Calidad"],
    "aprobado": ["María González – Gerente SGSI"],
    "secciones": {"objetivo": ["Prueba de carpeta pedida"]},
}


def _con_pedido(mensaje, anterior="", respuesta_anterior=""):
    payload = {
        "metadata": {"mensaje_usuario": mensaje},
        "mensaje": mensaje,
        "pedido_anterior": anterior,
        "respuesta_anterior": respuesta_anterior,
    }
    return patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)})


class TestFirmantesDadosEnUnTurnoAnterior(unittest.TestCase):
    FIRMANTES = {
        "elaborado": ["Ana Pérez – Analista de Seguridad"],
        "validado": ["Luis Soto – Jefe de TI"],
        "aprobado": ["María Rojas – Gerente de Operaciones"],
    }
    PRIMER_MENSAJE = (
        "Crea un procedimiento SGSI de «Control de Acceso Lógico». Firmantes: Elaboró Ana Pérez (Analista de "
        "Seguridad), Revisó Luis Soto (Jefe de TI), Aprobó María Rojas (Gerente de Operaciones)."
    )

    def _payload(self, **extra):
        base = {
            "metadata": {"mensaje_usuario": "No hay documento fuente: créalo sin fuente."},
            "mensaje": "No hay documento fuente: créalo sin fuente.",
        }
        return patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({**base, "pedido_anterior": "sí", **extra})})

    def test_el_runner_junta_los_mensajes_del_usuario_sin_reset(self):
        ruta = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
        espec = importlib.util.spec_from_file_location("runner_previos_prueba", ruta)
        runner = importlib.util.module_from_spec(espec)
        espec.loader.exec_module(runner)
        historial = [
            {"role": "user", "content": self.PRIMER_MENSAJE},
            {"role": "assistant", "content": "Muestro el esquema"},
            {"role": "user", "content": "/reset"},
            {"role": "user", "content": [{"type": "text", "text": "sí"}]},
            {"role": "tool", "content": "x"},
        ]
        self.assertEqual(runner._pedidos_previos_del_usuario(historial), [self.PRIMER_MENSAJE, "sí"])
        self.assertEqual(runner._pedidos_previos_del_usuario([]), [])
        self.assertLessEqual(
            sum(len(t) for t in runner._pedidos_previos_del_usuario([{"role": "user", "content": "x" * 9000}])), 6000
        )
