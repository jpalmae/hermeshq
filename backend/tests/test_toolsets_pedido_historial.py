import unittest
from types import SimpleNamespace

from hermeshq.services.toolsets_pedido import filtrar_para_tarea

AGENTE = [
    "safe",
    "file",
    "skills",
    "memory",
    "todo",
    "code_execution",
    "vision",
    "delegation",
    "messaging",
    "hermeshq_comms",
    "hermeshq_schedule",
    "hermeshq_xlsx",
    "hermeshq_documentos",
    "hermeshq_pptx",
    "hermeshq_docx",
    "hermeshq_pdf",
    "hermeshq_ms365_mail",
    "hermeshq_sharepoint",
]


def _tarea(prompt):
    return SimpleNamespace(prompt=prompt, metadata_json={"source": "public_chat", "conversation": True})


class SeguimientoConservaLasHerramientasDelTema(unittest.TestCase):
    def test_subir_a_sharepoint_el_excel_de_la_conversacion_conserva_las_herramientas_de_excel(self):
        historial = [
            {"role": "user", "content": "Analiza el Excel de reprocesos y agrega una hoja de resumen"},
            {"role": "assistant", "content": "Listo, agregué la hoja Resumen."},
        ]
        elegidos = filtrar_para_tarea(_tarea("Ahora súbelo a SharePoint"), AGENTE, historial)
        self.assertIn("hermeshq_sharepoint", elegidos)
        self.assertIn("hermeshq_xlsx", elegidos)
        self.assertIn("hermeshq_documentos", elegidos)

    def test_convertir_a_pdf_el_documento_word_conserva_docx(self):
        historial = [{"role": "user", "content": "Redacta el procedimiento en Word"}]
        elegidos = filtrar_para_tarea(_tarea("pásalo a PDF"), AGENTE, historial)
        self.assertIn("hermeshq_pdf", elegidos)
        self.assertIn("hermeshq_docx", elegidos)

    def test_sin_historial_el_filtro_no_cambia(self):
        elegidos = filtrar_para_tarea(_tarea("Ahora súbelo a SharePoint"), AGENTE, None)
        self.assertNotIn("hermeshq_xlsx", elegidos)
        self.assertEqual(elegidos, filtrar_para_tarea(_tarea("Ahora súbelo a SharePoint"), AGENTE))

    def test_solo_cuentan_los_ultimos_mensajes_del_usuario(self):
        historial = [{"role": "user", "content": "prepara una presentación"}] + [
            {"role": "user", "content": f"cambio menor {n}"} for n in range(3)
        ]
        elegidos = filtrar_para_tarea(_tarea("envíalo por correo"), AGENTE, historial)
        self.assertNotIn("hermeshq_pptx", elegidos)
        self.assertIn("hermeshq_ms365_mail", elegidos)


if __name__ == "__main__":
    unittest.main()
