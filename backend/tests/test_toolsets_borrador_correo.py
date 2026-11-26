import unittest

from hermeshq.services.toolsets_pedido import toolsets_para_pedido

AGENTE = [
    "safe",
    "file",
    "hermeshq_documentos",
    "hermeshq_ms365_mail",
    "hermeshq_ms365_calendar",
    "hermeshq_sharepoint",
]


class TestBorradorCargaCorreo(unittest.TestCase):
    def test_borrador_sin_la_palabra_correo_carga_el_correo(self):
        pedido = (
            "Déjame un borrador (NO lo envíes) para usuario1@contoso.cl, sin firma, asunto «Prueba R149», "
            "con el texto «Borrador de prueba.»"
        )
        self.assertIn("hermeshq_ms365_mail", toolsets_para_pedido(pedido, AGENTE))

    def test_un_correo_nombrado_carga_el_correo(self):
        self.assertIn("hermeshq_ms365_mail", toolsets_para_pedido("Mándale las gracias a ana@contoso.cl", AGENTE))

    def test_borrador_y_no_lo_envies_sin_direccion_carga_el_correo(self):
        self.assertIn("hermeshq_ms365_mail", toolsets_para_pedido("Prepárame un borrador, no lo envíes", AGENTE))

    def test_control_negativo_un_borrador_de_documento_no_carga_el_correo(self):
        elegidos = toolsets_para_pedido("Hazme un borrador del informe en Word", AGENTE)
        self.assertNotIn("hermeshq_ms365_mail", elegidos)

    def test_control_negativo_sin_senales_no_carga_el_correo(self):
        self.assertNotIn("hermeshq_ms365_mail", toolsets_para_pedido("Cuánto es 2 + 2", AGENTE))


if __name__ == "__main__":
    unittest.main()
