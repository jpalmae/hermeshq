import unittest
from types import SimpleNamespace

from hermeshq.services.toolsets_pedido import filtrar_para_tarea, toolsets_para_pedido

AGENTE = [
    "safe",
    "browser",
    "file",
    "skills",
    "memory",
    "todo",
    "messaging",
    "delegation",
    "vision",
    "code_execution",
    "hermeshq_comms",
    "hermeshq_pdf",
    "hermeshq_audio",
    "hermeshq_docx",
    "hermeshq_pptx",
    "hermeshq_xlsx",
    "hermeshq_schedule",
    "hermeshq_documentos",
    "image_gen",
    "hermeshq_lote",
    "hermeshq_ms365_calendar",
    "hermeshq_ms365_mail",
    "hermeshq_sharepoint",
    "hermeshq_ms365_teams",
    "toolset_nuevo_desconocido",
]


class TestToolsetsPedido(unittest.TestCase):
    def test_excel_adjunto_no_carga_correo_ppt_ni_sharepoint(self):
        pedido = "[Archivos adjuntos, ya disponibles en tu workspace: - ReprocesosMtto.xlsx (1365 KB)] Analiza el Excel"
        elegidos = toolsets_para_pedido(pedido, AGENTE)
        self.assertIn("hermeshq_xlsx", elegidos)
        self.assertIn("hermeshq_documentos", elegidos)
        self.assertNotIn("hermeshq_ms365_mail", elegidos)
        self.assertNotIn("hermeshq_pptx", elegidos)
        self.assertNotIn("hermeshq_sharepoint", elegidos)
        self.assertNotIn("hermeshq_ms365_teams", elegidos)

    def test_excel_nombrado_sin_adjunto_mantiene_sharepoint_para_buscarlo(self):
        pedido = "En el siguiente archivo en la ubicación ReprocesosMtto.xlsx tiene 2 hojas"
        elegidos = toolsets_para_pedido(pedido, AGENTE)
        self.assertIn("hermeshq_sharepoint", elegidos)
        self.assertIn("hermeshq_xlsx", elegidos)

    def test_caso_real_compartir_un_enlace_carga_sharepoint(self):
        for pedido in (
            "Compárteme un enlace de solo lectura para mi organización del Excel Ventas Ñuñoa Peñalolén 2026 que hiciste.",
            "dame el link de ese archivo",
            "súbelo a la carpeta del proyecto",
        ):
            self.assertIn("hermeshq_sharepoint", toolsets_para_pedido(pedido, AGENTE), pedido)

    def test_caso_real_informe_sin_adjunto_carga_sharepoint_para_la_fuente(self):
        for pedido in (
            "Genera un reporte de accidentabilidad de la Sucursal Norte (SUC1)",
            "Hazme un dashboard de licencias por sucursal",
            "Quiero un informe HSE de Centro",
        ):
            elegidos = toolsets_para_pedido(pedido, AGENTE)
            self.assertIn("hermeshq_sharepoint", elegidos, pedido)
            self.assertIn("hermeshq_documentos", elegidos, pedido)
            self.assertNotIn("hermeshq_ms365_mail", elegidos, pedido)

    def test_pedido_de_analisis_sin_adjunto_carga_sharepoint(self):
        self.assertIn(
            "hermeshq_sharepoint", toolsets_para_pedido("Necesito el análisis de los indicadores del trimestre", AGENTE)
        )

    def test_hablar_de_un_informe_sin_pedir_uno_no_carga_sharepoint(self):
        for pedido in ("Guarda el informe de ventas en el escritorio para mañana", "¿El informe de ayer quedó bien?"):
            self.assertNotIn("hermeshq_sharepoint", toolsets_para_pedido(pedido, AGENTE), pedido)

    def test_informe_con_adjunto_no_carga_sharepoint(self):
        pedido = "[Archivos adjuntos, ya disponibles en tu workspace: - eventos.xlsx (80 KB)] Genera el reporte de accidentabilidad"
        self.assertNotIn("hermeshq_sharepoint", toolsets_para_pedido(pedido, AGENTE))

    def test_caso_real_6fc4615d_procedimiento_sgsi_tras_reset_carga_el_creador_de_documentos(self):
        pedido = (
            "Crea un procedimiento del SGSI para el respaldo y la restauración de información de los servidores de "
            "Empresa. Adjúntalo aquí."
        )
        elegidos = toolsets_para_pedido(pedido, AGENTE)
        self.assertIn("hermeshq_documentos", elegidos)
        self.assertIn("hermeshq_docx", elegidos)
        for pedido in ("hazme una política de contraseñas", "arma el manual de uso del portal"):
            self.assertIn("hermeshq_documentos", toolsets_para_pedido(pedido, AGENTE), pedido)

    def test_presentacion_carga_pptx_y_no_correo(self):
        elegidos = toolsets_para_pedido("hazme una presentación del comité de tecnología", AGENTE)
        self.assertIn("hermeshq_pptx", elegidos)
        self.assertNotIn("hermeshq_ms365_mail", elegidos)
        self.assertNotIn("hermeshq_xlsx", elegidos)

    def test_correo_carga_mail_y_no_excel(self):
        elegidos = toolsets_para_pedido("revisa mis correos pendientes de hoy", AGENTE)
        self.assertIn("hermeshq_ms365_mail", elegidos)
        self.assertNotIn("hermeshq_xlsx", elegidos)
        self.assertNotIn("hermeshq_pptx", elegidos)

    def test_sin_senal_no_carga_integraciones_de_datos(self):
        resultado = toolsets_para_pedido("hola, cómo estás", AGENTE)
        self.assertNotIn("hermeshq_sharepoint", resultado)
        self.assertNotIn("hermeshq_ms365_mail", resultado)
        self.assertIn("hermeshq_comms", resultado)
        self.assertIn("toolset_nuevo_desconocido", resultado)

    def test_toolsets_desconocidos_se_conservan(self):
        self.assertIn("toolset_nuevo_desconocido", toolsets_para_pedido("analiza este excel", AGENTE))

    def test_tarea_programada_no_se_filtra(self):
        tarea = SimpleNamespace(
            prompt="Ejecuta la skill correo-unificado-bruno con mode=hourly",
            metadata_json={"scheduled": True, "source": "scheduled"},
        )
        self.assertEqual(filtrar_para_tarea(tarea, AGENTE), AGENTE)

    def test_tarea_de_chat_si_se_filtra(self):
        tarea = SimpleNamespace(
            prompt="[Archivos adjuntos, ya disponibles] ReprocesosMtto.xlsx: analiza",
            metadata_json={"source": "public_chat"},
        )
        self.assertNotIn("hermeshq_ms365_mail", filtrar_para_tarea(tarea, AGENTE))

    def test_respuesta_a_pregunta_no_se_filtra(self):
        self.assertEqual(toolsets_para_pedido("Sí, nómbralo Prueba y guarda en OneDrive", AGENTE), AGENTE)

    def test_seguimiento_no_se_filtra(self):
        pedido = "[Respuesta corta a tu último mensaje: «Listo»]\nvuelve a hacerlo"
        self.assertEqual(toolsets_para_pedido(pedido, AGENTE), AGENTE)

    def test_lista_vacia_o_none_se_devuelve_igual(self):
        self.assertIsNone(toolsets_para_pedido("excel", None))
        self.assertEqual(toolsets_para_pedido("excel", []), [])


if __name__ == "__main__":
    unittest.main()


class TestTrabajoLargoSiempreDisponible(unittest.TestCase):
    def test_un_pedido_de_dashboard_conserva_hq_trabajo_largo(self):
        from hermeshq.services.toolsets_pedido import toolsets_para_pedido

        elegidos = toolsets_para_pedido("arregla mi dashboard de licencias", AGENTE)
        self.assertIn("hermeshq_schedule", elegidos)
        self.assertIn("hermeshq_documentos", elegidos)

    def test_un_pedido_de_documento_conserva_hq_trabajo_largo(self):
        from hermeshq.services.toolsets_pedido import toolsets_para_pedido

        self.assertIn("hermeshq_schedule", toolsets_para_pedido("hazme una presentacion del comite", AGENTE))
