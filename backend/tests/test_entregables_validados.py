import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services.formato_pedido import (
    DIRECTIVA_CHAT,
    DIRECTIVA_EXCEL,
    DIRECTIVA_HTML,
    extensiones_pedidas,
    formato_de_la_directiva,
)

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


SP = _cargar("sharepoint_formato_pedido", _RAIZ / "integration_packages" / "sharepoint" / "plugin" / "__init__.py")
DOCS = _cargar("documentos_entregables", _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py")


class TestFormatoPedido(unittest.TestCase):
    def test_directivas(self):
        self.assertEqual(formato_de_la_directiva(DIRECTIVA_HTML + "\nhazme el informe"), "html")
        self.assertEqual(formato_de_la_directiva(DIRECTIVA_EXCEL + "\nx"), "excel")
        self.assertEqual(formato_de_la_directiva(DIRECTIVA_CHAT + "\nx"), "chat")
        self.assertIsNone(formato_de_la_directiva("hazme el informe"))
        self.assertEqual(extensiones_pedidas(DIRECTIVA_HTML), frozenset({".html", ".htm"}))
        self.assertIsNone(extensiones_pedidas(DIRECTIVA_CHAT))
        self.assertIsNone(extensiones_pedidas("sin directiva"))

    def test_el_runner_lo_pasa_a_las_herramientas(self):
        self.assertEqual(runner._formato_pedido({"prompt": DIRECTIVA_HTML + "\nx"}), "html")
        self.assertEqual(runner._formato_pedido({"prompt": "hola"}), "")


class TestSubidaSoloDelFormatoPedido(unittest.TestCase):
    def _con_formato(self, formato):
        return patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"formato_pedido": formato})})

    def test_caso_real_pidio_html_y_el_pdf_generado_no_se_sube(self):
        with self._con_formato("html"):
            error = SP._formato_no_pedido(Path("/ws/outputs/Estructura_Report_Server_V3.pdf"))
        self.assertIn("HTML", error)
        with self._con_formato("html"):
            self.assertIsNone(SP._formato_no_pedido(Path("/ws/outputs/Estructura_Report_Server_V3.html")))

    def test_sin_formato_pedido_o_archivo_del_usuario_no_se_restringe(self):
        with self._con_formato(""):
            self.assertIsNone(SP._formato_no_pedido(Path("/ws/outputs/x.pdf")))
        with self._con_formato("html"):
            self.assertIsNone(SP._formato_no_pedido(Path("/ws/uploads/contrato.pdf")))
