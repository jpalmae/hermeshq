import unittest

from hermeshq.services.adjunto_pendiente import menciona_adjunto_ausente

PEDIDO_DEL_14_11 = (
    "Hazlo como trabajo en segundo plano: arma un informe interactivo HTML sobre estos tickets de soporte "
    "(Mes | Abiertos | Cerrados: Agosto 42 38, Septiembre 55 47, Octubre 61 59, Noviembre 48 40) y avísame aquí "
    "con el archivo adjunto cuando termine. No lo subas a OneDrive."
)
PEDIDO_DE_ANA_11_11 = (
    "Hace un informe interactivo de 1 sola pagina sin resumen solo 1 pagina de contenido explicando lo que sale en "
    "la imagen con titulo Estructura Demo. Además, agregar dos árbol jerárquico uno de Empresa."
)


class TestAdjuntoQueSeEntrega(unittest.TestCase):
    def test_el_pedido_del_14_11_no_pide_un_adjunto(self):
        self.assertIsNone(menciona_adjunto_ausente(PEDIDO_DEL_14_11))

    def test_otras_formas_de_pedir_recibir_el_archivo(self):
        for pedido in (
            "Prepárame el informe y avísame con el archivo adjunto.",
            "Cuando termines, envíame el archivo adjunto por aquí.",
            "Arma el gráfico y mándame la imagen cuando termine.",
            "Genera el resumen y entrégame el archivo adjunto al terminar.",
            "Dime cuando esté listo, con el archivo adjunto cuando termine.",
        ):
            with self.subTest(pedido=pedido):
                self.assertIsNone(menciona_adjunto_ausente(pedido))

    def test_el_pedido_de_ana_con_una_imagen_que_no_llego_sigue_avisando(self):
        self.assertEqual(menciona_adjunto_ausente(PEDIDO_DE_ANA_11_11), "la imagen")

    def test_los_adjuntos_de_entrada_siguen_avisando(self):
        for pedido, esperado in (
            ("Revisa el archivo adjunto y resúmelo.", "el archivo adjunto"),
            ("Analiza esta imagen y dime qué ves.", "la imagen"),
            ("Envíame un resumen de la imagen.", "la imagen"),
            ("Con el archivo adjunto arma el informe, y avísame cuando esté listo.", "el archivo adjunto"),
            ("Explica el pdf adjunto.", "el archivo adjunto"),
        ):
            with self.subTest(pedido=pedido):
                self.assertEqual(menciona_adjunto_ausente(pedido), esperado)

    def test_un_adjunto_que_si_llego_no_avisa(self):
        nota = "[Archivos adjuntos: foto.png] "
        self.assertIsNone(menciona_adjunto_ausente(nota + "Explica la imagen"))


if __name__ == "__main__":
    unittest.main()
