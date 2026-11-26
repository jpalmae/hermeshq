import unittest

from hermeshq.services.public_chat_service import es_pedido_largo


class TestPedidoLargo(unittest.TestCase):
    def test_los_pedidos_de_informe_van_a_segundo_plano(self):
        for texto in (
            "Necesito el informe interactivo por rol del ang p67",
            "Genera un informe interactivo por rol para procedimiento SUC-P-67",
            "hazme un informe por niveles de este procedimiento",
            "Y mi informe interactivo por rol del ang p67?",
        ):
            with self.subTest(texto=texto):
                self.assertTrue(es_pedido_largo(texto))

    def test_crear_o_analizar_un_excel_va_a_segundo_plano(self):
        for texto in (
            "créame un Excel con ventas mensuales de enero a junio",
            "analiza el Excel de ventas que te mandé",
            "resume la planilla adjunta por región",
            "pásalo a Excel",
            "compárala con la planilla de marzo",
            "compáralos con el Excel del mes pasado",
        ):
            with self.subTest(texto=texto):
                self.assertTrue(es_pedido_largo(texto))

    def test_mover_un_excel_no_es_un_informe_largo(self):
        for texto in (
            'sube el Excel de ventas que hiciste a mi OneDrive, en una carpeta "Pruebas HermesHQ", con el nombre '
            '"Ventas H1 región ñandú.xlsx"',
            "envíame el Excel de nuevo",
            "comparte el Excel con Ana",
            "Compárteme un enlace de solo lectura para mi organización del Excel Ventas Ñuñoa 2026 que hiciste.",
            "compártelo con Pedro, el Excel de ventas",
            "súbelo a OneDrive, el Excel",
            "mándame el Excel por correo",
            "dame el link del Excel",
        ):
            with self.subTest(texto=texto):
                self.assertFalse(es_pedido_largo(texto))

    def test_caso_real_excel_con_los_datos_en_el_mensaje_sigue_por_el_chat(self):
        for texto in (
            "Hazme un Excel con las ventas de enero a marzo de 3 sucursales (Norte 120, 135, 150; Centro 200, 190, "
            "210; Sur 80, 95, 110) y un gráfico de evolución por sucursal.",
            "Crea un Excel con estos totales: enero 1.200, febrero 1.350, marzo 1.410, abril 990, mayo 1.020, junio 1.100",
        ):
            with self.subTest(texto=texto[:40]):
                self.assertFalse(es_pedido_largo(texto))

    def test_datos_en_el_mensaje_pero_con_un_archivo_sigue_en_segundo_plano(self):
        for texto in (
            "Resume el Excel adjunto y agrega estas ventas: Norte 120, 135, 150; Centro 200, 190, 210",
            "Crea un Excel cruzando la planilla de marzo con Norte 120, 135, 150 y Centro 200, 190, 210",
            "Analiza ventas.xlsx y compáralo con Norte 120, 135, 150; Centro 200, 190, 210",
            "Necesito el informe interactivo por rol con Excel: 1, 2, 3, 4, 5, 6",
        ):
            with self.subTest(texto=texto[:40]):
                self.assertTrue(es_pedido_largo(texto))

    def test_el_resto_de_los_mensajes_sigue_por_el_chat(self):
        for texto in ("si", "/reset", "Cuántos correos tengo hoy?", "Envía un correo a Ana", None, ""):
            with self.subTest(texto=texto):
                self.assertFalse(es_pedido_largo(texto))


if __name__ == "__main__":
    unittest.main()
