import unittest
from types import SimpleNamespace

from hermeshq.services.agent_supervisor import history_assistant_text
from hermeshq.services.respuesta_verificada import (
    AVISO_SIN_ADJUNTO,
    con_aviso_de_adjunto,
    sin_avisos_de_la_plataforma,
)

PROPUESTA = "Esquema listo.\n\n¿Aplico con estos datos de ejemplo y adjunto el .docx + PDF aquí (sin subir a OneDrive)?"
AVISO_REVISION = "⚠️ Revisé lo que se ejecutó y corregí esta respuesta: No se guardó ni subió ningún archivo. Si lo necesitas, pídemelo de nuevo."


class TestAvisoDeAdjuntoNoEnPropuestas(unittest.TestCase):
    def test_una_propuesta_con_pregunta_no_recibe_el_aviso(self):
        self.assertEqual(con_aviso_de_adjunto(PROPUESTA), PROPUESTA)

    def test_control_negativo_una_promesa_sin_archivo_si_lo_recibe(self):
        texto = "Listo, te adjunto el archivo con el resumen."
        self.assertTrue(con_aviso_de_adjunto(texto).endswith(AVISO_SIN_ADJUNTO))

    def test_una_promesa_junto_a_una_pregunta_distinta_sigue_avisando(self):
        texto = "¿Te sirve? Te adjunto el archivo ahora."
        self.assertIn(AVISO_SIN_ADJUNTO, con_aviso_de_adjunto(texto))

    def test_con_media_no_hay_aviso(self):
        texto = "Te adjunto el archivo.\n\nMEDIA: outputs/a.docx"
        self.assertEqual(con_aviso_de_adjunto(texto), texto)


class TestHistorialSinAvisosDeLaPlataforma(unittest.TestCase):
    def test_quita_el_aviso_final_de_adjunto(self):
        texto = f"{PROPUESTA}\n\n{AVISO_SIN_ADJUNTO}"
        self.assertEqual(sin_avisos_de_la_plataforma(texto), PROPUESTA)

    def test_quita_el_aviso_inicial_de_revision(self):
        texto = f"{AVISO_REVISION}\n\nNo lo envié, tal como pediste."
        self.assertEqual(sin_avisos_de_la_plataforma(texto), "No lo envié, tal como pediste.")

    def test_no_toca_una_respuesta_sin_avisos(self):
        self.assertEqual(
            sin_avisos_de_la_plataforma("⚠️ Importante: revisa las cifras."), "⚠️ Importante: revisa las cifras."
        )

    def test_si_solo_hay_aviso_se_conserva(self):
        self.assertEqual(sin_avisos_de_la_plataforma(AVISO_SIN_ADJUNTO), AVISO_SIN_ADJUNTO)

    def test_el_historial_que_recibe_el_modelo_no_trae_el_aviso(self):
        tarea = SimpleNamespace(status="completed", error_message=None)
        historial = history_assistant_text(tarea, f"{PROPUESTA}\n\n{AVISO_SIN_ADJUNTO}", 0)
        self.assertNotIn("reintenta", historial)
        self.assertEqual(historial, PROPUESTA)

    def test_el_recorte_se_aplica_despues_de_quitar_el_aviso(self):
        tarea = SimpleNamespace(status="completed", error_message=None)
        historial = history_assistant_text(tarea, f"{PROPUESTA}\n\n{AVISO_SIN_ADJUNTO}", len(PROPUESTA) + 3)
        self.assertEqual(historial, PROPUESTA)


if __name__ == "__main__":
    unittest.main()


class TestRecorteConservaElFinal:
    def test_el_recorte_deja_el_principio_y_el_final(self):
        tarea = SimpleNamespace(status="completed", error_message=None)
        texto = "INICIO " + "x" * 5000 + " ¿Lo envío?\nMEDIA: outputs/a.pdf"
        recortado = history_assistant_text(tarea, texto, 1000)
        assert recortado.startswith("INICIO") and recortado.endswith("MEDIA: outputs/a.pdf")
        assert "¿Lo envío?" in recortado and "caracteres omitidos" in recortado
        assert len(recortado) < 1200

    def test_control_negativo_un_texto_corto_no_se_toca(self):
        tarea = SimpleNamespace(status="completed", error_message=None)
        assert history_assistant_text(tarea, "hola", 1000) == "hola"

    def test_sin_limite_no_recorta(self):
        tarea = SimpleNamespace(status="completed", error_message=None)
        assert history_assistant_text(tarea, "y" * 9000, 0) == "y" * 9000
