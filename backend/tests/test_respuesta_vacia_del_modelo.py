import importlib.util
import unittest
from pathlib import Path

from hermeshq.services.schedule_notifications import friendly_error_text

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_respuesta_vacia_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

CENTINELA = {"role": "assistant", "content": "(empty)", "_empty_terminal_sentinel": True}


def _llamada(nombre: str) -> dict:
    return {"id": f"call-{nombre}", "type": "function", "function": {"name": nombre, "arguments": "{}"}}


def _resultado(mensajes: list, final: str = "(empty)") -> dict:
    return {"final_response": final, "messages": mensajes, "completed": True}


class RespuestaVaciaDelModelo(unittest.TestCase):
    def setUp(self):
        runner._HERRAMIENTAS_INICIADAS.clear()
        self.addCleanup(runner._HERRAMIENTAS_INICIADAS.clear)

    def test_si_hermes_reescribio_el_historial_las_herramientas_iniciadas_se_siguen_nombrando(self):
        runner._HERRAMIENTAS_INICIADAS.extend(["ms365_mail_send", "ms365_mail_send", "read_file"])
        mensajes = [{"role": "user", "content": "Envía el correo y confirma"}, CENTINELA]
        final, limpios, _ = runner._cerrar_turno(_resultado(mensajes), [])
        self.assertIn("ms365_mail_send", final)
        self.assertIn("read_file", final)
        self.assertEqual(final.count("ms365_mail_send"), 1)
        self.assertNotIn("(empty)", final)
        self.assertEqual(limpios[-1], {"role": "assistant", "content": final})

    def test_sin_herramientas_la_tarea_falla_en_vez_de_entregar_el_centinela(self):
        mensajes = [{"role": "user", "content": "Ahora arma un PDF de una página"}, CENTINELA]
        with self.assertRaises(RuntimeError) as contexto:
            runner._cerrar_turno(_resultado(mensajes), [])
        self.assertIn("empty_response_exhausted", str(contexto.exception))
        self.assertIn("stopped early", str(contexto.exception))

    def test_el_usuario_recibe_una_explicacion_y_no_la_de_llamada_invalida(self):
        error = "Hermes runtime stopped early: empty_response_exhausted: el modelo devolvió respuestas vacías"
        texto = friendly_error_text(error)
        self.assertIn("no devolvió respuesta", texto)
        self.assertIn("no se ejecutó nada", texto)
        self.assertNotIn("llamada inválida", texto)
        self.assertNotIn("(empty)", texto)

    def test_con_herramientas_ya_ejecutadas_el_aviso_las_nombra_y_no_afirma_exito(self):
        mensajes = [
            {"role": "user", "content": "Genera el informe y súbelo"},
            {"role": "assistant", "content": "", "tool_calls": [_llamada("hq_docx_generate"), _llamada("web_search")]},
            {"role": "tool", "name": "hq_docx_generate", "content": '{"success": true}'},
            CENTINELA,
        ]
        final, limpios, llamadas = runner._cerrar_turno(_resultado(mensajes), [])
        self.assertNotIn("(empty)", final)
        self.assertIn("hq_docx_generate", final)
        self.assertIn("web_search", final)
        self.assertIn("No sé si el resultado quedó completo", final)
        self.assertEqual(limpios[-1], {"role": "assistant", "content": final})
        self.assertFalse(any(m.get("_empty_terminal_sentinel") for m in limpios))
        self.assertEqual([c["name"] for c in llamadas], ["hq_docx_generate", "web_search"])

    def test_solo_cuentan_las_herramientas_del_turno_actual(self):
        mensajes = [
            {"role": "user", "content": "Crea el informe"},
            {"role": "assistant", "content": "", "tool_calls": [_llamada("hq_docx_generate")]},
            {"role": "tool", "name": "hq_docx_generate", "content": '{"success": true}'},
            {"role": "assistant", "content": "Listo."},
            {"role": "user", "content": "Ahora arma un PDF"},
            CENTINELA,
        ]
        with self.assertRaises(RuntimeError) as contexto:
            runner._cerrar_turno(_resultado(mensajes), [])
        self.assertIn("empty_response_exhausted", str(contexto.exception))

    def test_una_respuesta_normal_no_cambia(self):
        mensajes = [{"role": "user", "content": "Hola"}, {"role": "assistant", "content": "Hola, ¿en qué te ayudo?"}]
        final, limpios, _ = runner._cerrar_turno(_resultado(mensajes, "Hola, ¿en qué te ayudo?"), [])
        self.assertEqual(final, "Hola, ¿en qué te ayudo?")
        self.assertEqual(limpios, mensajes)

    def test_un_texto_que_solo_menciona_empty_no_se_confunde(self):
        texto = "Los campos (empty) del formulario se rellenan con el valor por defecto."
        self.assertFalse(runner.es_respuesta_vacia_del_motor(texto))
        self.assertTrue(runner.es_respuesta_vacia_del_motor(" (empty)\n"))
        self.assertFalse(runner.es_respuesta_vacia_del_motor(None))
        self.assertFalse(runner.es_respuesta_vacia_del_motor(""))


if __name__ == "__main__":
    unittest.main()
