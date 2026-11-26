from hermeshq.services.toolsets_pedido import toolsets_para_pedido

HABILITADOS = ["safe", "file", "hermeshq_comms", "hermeshq_sharepoint", "hermeshq_documentos", "hermeshq_ms365_mail"]


def incluye_sharepoint(texto: str) -> bool:
    return "hermeshq_sharepoint" in (toolsets_para_pedido(texto, HABILITADOS) or [])


class TestDisparoDeSharePoint:
    def test_guardar_un_informe_local_no_activa_sharepoint(self):
        assert not incluye_sharepoint("Guarda el informe de ventas en el escritorio para mañana")

    def test_la_palabra_guardar_sola_no_activa_sharepoint(self):
        assert not incluye_sharepoint("Guardaste bien el dato que te di?")

    def test_pedido_explicito_de_sharepoint_activa_la_herramienta(self):
        assert incluye_sharepoint("Busca el contrato de proveedor en SharePoint")

    def test_mencion_de_onedrive_activa_la_herramienta_residual_documentado(self):
        assert incluye_sharepoint("¿Guardaste hoy un resumen en mi carpeta de OneDrive?")
