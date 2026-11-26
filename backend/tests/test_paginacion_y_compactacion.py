import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

import hermeshq.scripts.hermes_task_runner as runner

_PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermeshq_documentos_paginacion", _PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class _Agente:
    def __init__(self):
        self.context_compressor = SimpleNamespace(compression_count=0)
        self.interrupciones = 0

    def interrupt(self, message=None):
        self.interrupciones += 1


class TestCorteDeCompactacionContinua(unittest.TestCase):
    def test_caso_real_compactacion_en_cada_paso_se_corta(self):
        guardia = runner.GuardiaDesbocada({"activa": True, "max_compactaciones": 25})
        agente = _Agente()
        guardia.vincular(agente)
        for paso in range(1, 60):
            agente.context_compressor.compression_count = paso
            guardia.paso(paso)
        self.assertEqual(agente.interrupciones, 1)
        self.assertEqual(guardia.disparo["motivo"], "compactacion")
        self.assertEqual(guardia.disparo["paso"], 26)
        self.assertIn("se compactó una y otra vez", guardia.aviso())

    def test_pocas_compactaciones_no_cortan(self):
        guardia = runner.GuardiaDesbocada({"activa": True})
        agente = _Agente()
        guardia.vincular(agente)
        for paso in range(1, 40):
            agente.context_compressor.compression_count = min(paso // 10, 3)
            guardia.paso(paso)
        self.assertIsNone(guardia.disparo)
        self.assertEqual(agente.interrupciones, 0)

    def test_desactivada_o_sin_compresor(self):
        guardia = runner.GuardiaDesbocada({"activa": False})
        agente = _Agente()
        agente.context_compressor.compression_count = 500
        guardia.vincular(agente)
        guardia.paso(1)
        self.assertIsNone(guardia.disparo)
        sin = runner.GuardiaDesbocada({"activa": True})
        sin.vincular(SimpleNamespace(interrupt=lambda message=None: None))
        sin.paso(1)
        self.assertIsNone(sin.disparo)
