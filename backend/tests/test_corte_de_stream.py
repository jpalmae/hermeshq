import importlib.util
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_corte_stream_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

AVISO = (
    "\n\n⚠ Stream stalled mid tool-call (ms365_correo_sync); the action was not executed. "
    "Ask me to retry if you want to continue."
)


class CorteDeStreamEnUnaLlamadaAHerramienta(unittest.TestCase):
    def test_el_aviso_se_traduce_y_no_queda_ingles(self):
        salida = runner.limpiar_aviso_interno("Avance parcial del informe." + AVISO)
        self.assertIn("Avance parcial del informe.", salida)
        self.assertIn("ms365_correo_sync", salida)
        self.assertIn("no se ejecutó", salida)
        for resto in ("Stream stalled", "the action was not executed", "Ask me to retry"):
            self.assertNotIn(resto, salida)

    def test_solo_el_aviso_es_una_tarea_sin_resultado(self):
        self.assertEqual(runner.corte_de_stream_sin_contenido(AVISO.strip()), "ms365_correo_sync")
        self.assertEqual(runner.corte_de_stream_sin_contenido(AVISO), "ms365_correo_sync")

    def test_con_contenido_util_no_se_trata_como_fallo(self):
        texto = "Resumen de la bandeja: 3 correos nuevos de Finanzas y 1 de Operaciones." + AVISO
        self.assertIsNone(runner.corte_de_stream_sin_contenido(texto))

    def test_sin_aviso_no_hay_corte(self):
        self.assertIsNone(runner.corte_de_stream_sin_contenido("Informe listo."))
        self.assertIsNone(runner.corte_de_stream_sin_contenido(""))


if __name__ == "__main__":
    unittest.main()


class BarraEscapadaEnTablas(unittest.TestCase):
    def test_caso_real_la_barra_escapada_de_una_fila_no_corta_la_columna(self):
        fila = "| 11:00 – 12:00 | Curso \\| Ley Karin y Riesgos Psicosociales | Empresa Informa | Pendiente |"
        limpia = runner.limpiar_aviso_interno(fila)
        self.assertEqual(limpia.count("|"), 5)
        self.assertIn("Curso ∣ Ley Karin", limpia)

    def test_fuera_de_una_tabla_no_se_toca(self):
        comando = "grep -E 'error\\|warning' app.log"
        self.assertEqual(runner.limpiar_aviso_interno(comando), comando)
