import unittest

from hermeshq.services.respuesta_verificada import AVISO_ENLACE_INVENTADO, con_aviso_sin_herramienta

CASO_REAL = (
    "El archivo `equipo-r119.pptx` ya está en tu espacio de trabajo. Generé un enlace para compartirlo:\n\n"
    "🔗 https://enlace.example/7p0s2\n\nEs de acceso general dentro de tu organización."
)


class TestEnlaceInventado(unittest.TestCase):
    def test_caso_real_cad0ad91_el_enlace_inventado_se_quita(self):
        r = con_aviso_sin_herramienta(CASO_REAL, 0, "Compártelo con admin@proveedor.com")
        self.assertNotIn("enlace.example", r)
        self.assertIn("[enlace no verificado]", r)
        self.assertIn(AVISO_ENLACE_INVENTADO, r)

    def test_control_negativo_con_herramientas_usadas_el_enlace_se_conserva(self):
        self.assertEqual(con_aviso_sin_herramienta(CASO_REAL, 1, ""), CASO_REAL)

    def test_un_enlace_que_el_usuario_escribio_no_se_toca(self):
        texto = "Generé un resumen de https://learn.microsoft.com/es-es/graph"
        r = con_aviso_sin_herramienta(texto, 0, "mira https://learn.microsoft.com/es-es/graph")
        self.assertNotIn("[enlace no verificado]", r)

    def test_citar_una_documentacion_sin_afirmar_haberla_creado_no_se_toca(self):
        texto = "Puedes ver la guía en https://learn.microsoft.com/es-es/graph"
        self.assertEqual(con_aviso_sin_herramienta(texto, 0, ""), texto)
