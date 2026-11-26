from hermeshq.services.toolsets_pedido import toolsets_para_pedido

HABILITADOS = [
    "safe",
    "file",
    "hermeshq_comms",
    "hermeshq_sharepoint",
    "hermeshq_documentos",
    "hermeshq_ms365_mail",
    "hermeshq_ms365_calendar",
    "hermeshq_ms365_teams",
]


def toolsets(texto: str) -> list[str]:
    return toolsets_para_pedido(texto, HABILITADOS) or []


class TestBusquedaDeArchivo:
    def test_caso_real_busca_matriz_de_priorizacion(self):
        assert "hermeshq_sharepoint" in toolsets("busca matriz de priorización")

    def test_otros_pedidos_reales_de_busqueda(self):
        for texto in (
            "busca este documento sgsi-pro-dss-08",
            "busca la query que me diste sobre sigco",
            "busca todo lo relacionado con proyecto migración ERP hacia AWS",
            "pero el que acabas de crear donde quedó",
            "encuéntrame el informe de accidentabilidad",
            "localiza la presentación del comité",
        ):
            assert "hermeshq_sharepoint" in toolsets(texto), texto

    def test_con_adjunto_no_se_agrega(self):
        assert "hermeshq_sharepoint" not in toolsets(
            "[Archivos adjuntos, ya disponibles en tu workspace: x.pdf] busca la cifra del total"
        )

    def test_busqueda_de_correo_o_calendario_no_agrega_sharepoint(self):
        for texto in (
            "busca en mi bandeja de entrada el correo con asunto bateria c4",
            "busca la reunión prueba asistentebot y dime quiénes confirmaron",
            "busca en teams si tengo algún chat sin responder",
        ):
            assert "hermeshq_sharepoint" not in toolsets(texto), texto

    def test_sin_senal_de_busqueda_sigue_sin_sharepoint(self):
        for texto in ("hola, ¿cómo estás?", "resume este texto en tres líneas", "qué hora es"):
            assert "hermeshq_sharepoint" not in toolsets(texto), texto

    def test_boilerplate_del_sistema_no_dispara(self):
        assert "hermeshq_sharepoint" not in toolsets(
            "dame un resumen. Trabaja sobre él; no digas que no tienes el contenido. Eres quien buscaste antes."
        )
