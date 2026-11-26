import unittest

from hermeshq.services.execution_lease import mensaje_de_fallo


class MensajeDeFalloParaElUsuario(unittest.TestCase):
    def test_interrumpida_dice_que_no_se_repitio_nada_y_que_puede_volver_a_pedirla(self):
        texto = mensaje_de_fallo("interrumpida")
        self.assertIn("No se repitió nada", texto)
        self.assertIn("puedes volver a pedirla", texto)
        self.assertNotIn("no se reintenta", texto)

    def test_resultado_incierto_advierte_del_riesgo_de_duplicar_acciones(self):
        texto = mensaje_de_fallo("resultado_incierto")
        self.assertIn("duplicados", texto)
        self.assertIn("revisa", texto)

    def test_otras_clases_conservan_el_mensaje_general(self):
        self.assertIn("sin titular", mensaje_de_fallo("lease_vencido"))


if __name__ == "__main__":
    unittest.main()
