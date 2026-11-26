import unittest

from hermeshq.services.background_style import REGLA_VERIFICACION
from hermeshq.services.respuesta_verificada import AVISO_SIN_HERRAMIENTA, con_aviso_sin_herramienta


class TestAvisoSinHerramienta(unittest.TestCase):
    def test_afirmar_que_se_genero_sin_herramientas_agrega_el_aviso(self):
        texto = "¡Listo! 🐱 Generé un gato de cerca."
        self.assertEqual(con_aviso_sin_herramienta(texto, 0), f"{texto}\n\n{AVISO_SIN_HERRAMIENTA}")

    def test_negar_tener_terminal_sin_herramientas_agrega_el_aviso(self):
        texto = "Tienes razón, pero en este entorno no tengo terminal disponible."
        self.assertIn(AVISO_SIN_HERRAMIENTA, con_aviso_sin_herramienta(texto, 0))

    def test_con_herramientas_usadas_no_cambia(self):
        texto = "Generé el informe y lo verifiqué."
        self.assertEqual(con_aviso_sin_herramienta(texto, 2), texto)

    def test_respuesta_sin_afirmaciones_no_cambia(self):
        texto = "Puedo ayudarte con eso. ¿Qué formato prefieres?"
        self.assertEqual(con_aviso_sin_herramienta(texto, 0), texto)

    def test_vacio(self):
        self.assertIsNone(con_aviso_sin_herramienta(None, 0))
        self.assertEqual(con_aviso_sin_herramienta("", 0), "")


class TestReglaDeVerificacion(unittest.TestCase):
    def test_la_regla_prohibe_afirmar_sin_haber_llamado_la_herramienta(self):
        self.assertIn("no la llamaste en este turno", REGLA_VERIFICACION)
        self.assertIn("ni que no tienes una herramienta", REGLA_VERIFICACION)


if __name__ == "__main__":
    unittest.main()


class TestReglaHerramientas(unittest.TestCase):
    def test_la_regla_prohibe_inventar_terminal_y_nombres_vacios(self):
        from hermeshq.services.background_style import REGLA_HERRAMIENTAS

        self.assertIn("terminal", REGLA_HERRAMIENTAS)
        self.assertIn("execute_code", REGLA_HERRAMIENTAS)
        self.assertIn("vacío", REGLA_HERRAMIENTAS)
        self.assertIn("sharepoint_search", REGLA_HERRAMIENTAS)
        self.assertIn("vision_analyze", REGLA_HERRAMIENTAS)
        self.assertIn("hq_leer_documento", REGLA_HERRAMIENTAS)
        self.assertNotIn("pymupdf", REGLA_HERRAMIENTAS)
