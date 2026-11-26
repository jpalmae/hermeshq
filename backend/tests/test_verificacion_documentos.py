import importlib.util
import unittest
from pathlib import Path

from docx import Document

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
spec = importlib.util.spec_from_file_location("hermeshq_documentos_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

FUENTE = ["Pauta 4S mecánica: revisar frenos y temperatura de reductores.", "Lubricación 12S: revisar niveles."]


def _docx(ruta, parrafos):
    documento = Document()
    for texto in parrafos:
        documento.add_paragraph(texto)
    documento.save(ruta)


class ReglaDeFuentesEnElPrompt(unittest.TestCase):
    def test_la_regla_pide_decir_no_consta_y_citar_la_fuente(self):
        from hermeshq.services.background_style import REGLA_FUENTES

        self.assertIn("no consta en la fuente", REGLA_FUENTES)

    def test_la_regla_cubre_roles_codigos_y_reglas_de_exclusion(self):
        from hermeshq.services.background_style import REGLA_FUENTES

        self.assertIn("roles", REGLA_FUENTES)
        self.assertIn("«supuesto»", REGLA_FUENTES)
        self.assertIn("reglas de exclusión", REGLA_FUENTES)
        self.assertIn("categoría", REGLA_FUENTES)
