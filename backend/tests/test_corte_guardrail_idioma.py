import importlib.util
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_corte_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

CORTE_REAL = (
    "I stopped retrying read_file because it hit the tool-call guardrail (repeated_exact_failure_block) "
    "after 5 repeated non-progressing attempts. The last tool result explains the blocker; the next "
    "step is to change strategy instead of repeating the same call."
)


class CorteGuardrailEnEspanolTests(unittest.TestCase):
    def test_corte_en_ingles_se_traduce_sin_restos_en_ingles(self):
        salida = runner.limpiar_aviso_interno(CORTE_REAL)
        self.assertIn("read_file", salida)
        self.assertTrue(salida.startswith("No pude terminar esta parte"))
        for resto in ("stopped", "guardrail", "strategy", "blocker"):
            self.assertNotIn(resto, salida)

    def test_respuesta_final_con_corte_no_llega_en_ingles(self):
        resultado = {"final_response": "Primera parte lista. " + CORTE_REAL}
        salida = runner.texto_final(resultado, [])
        self.assertIn("Primera parte lista.", salida)
        self.assertNotIn("guardrail", salida)
        self.assertNotIn("stopped", salida)

    def test_respuesta_final_solo_con_corte_queda_util(self):
        salida = runner.texto_final({"final_response": CORTE_REAL}, [])
        self.assertTrue(salida.startswith("No pude terminar esta parte"))

    def test_fragmentos_en_streaming_tambien_se_limpian(self):
        salida = runner.texto_final({}, ["Hola. ", CORTE_REAL])
        self.assertIn("Hola.", salida)
        self.assertNotIn("guardrail", salida)

    def test_texto_normal_no_cambia(self):
        texto = "Informe listo: 12 filas y 3 hojas. Sin cambios."
        self.assertEqual(runner.texto_final({"final_response": texto}, []), texto)
