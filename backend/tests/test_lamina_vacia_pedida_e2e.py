import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_vacia_e2e", _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

PEDIDO = (
    "Hazme una presentación de 3 láminas: portada, una lámina con el título «Pendiente» pero sin contenido "
    "(déjala totalmente vacía, solo el título) y cierre."
)


class TestLaminaVaciaPedidaDeExtremoAExtremo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(
            os.environ,
            {
                "HERMESHQ_SKILL_TEMPLATES": str(_RAIZ / "skill_templates"),
                "HERMES_SESSION_CWD": self.tmp.name,
                "HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {"mensaje_usuario": PEDIDO}}),
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)

    def crear(self, lamina):
        spec = {"titulo": "Plan de prueba QA 3", "area": "Calidad", "diapositivas": [lamina]}
        return json.loads(plugin._crear_presentacion({"tipo": "informe-ejecutivo", "spec": spec}))

    def titulos(self, out):
        from pptx import Presentation

        ruta = Path(self.tmp.name) / out["archivos"][0]
        return [
            [s.text_frame.text for s in lam.shapes if s.has_text_frame and s.text_frame.text.strip()]
            for lam in Presentation(str(ruta)).slides
        ]

    def test_el_spec_real_del_agente_genera_el_archivo_con_pendiente_solo_con_titulo(self):
        out = self.crear(
            {"modelo": "contenido-blanco", "titulo": "Pendiente", "visual": {"tipo": "vinetas", "items": []}}
        )
        self.assertTrue(out["success"], out)
        laminas = self.titulos(out)
        pendiente = [t for t in laminas if "Pendiente" in t]
        self.assertEqual(len(pendiente), 1, laminas)
        self.assertEqual([x for x in pendiente[0] if x not in ("Pendiente", "contoso.cl")], [])

    def test_una_lamina_no_pedida_con_visual_vacio_se_sigue_rechazando(self):
        out = self.crear(
            {"modelo": "contenido-blanco", "titulo": "Riesgos", "visual": {"tipo": "vinetas", "items": []}}
        )
        self.assertFalse(out["success"], out)
