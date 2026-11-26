import unittest

from hermeshq.scripts import hermes_task_runner as runner

ANALISIS = (
    "Tengo el anexo. Ahora contrasto el correo de Pablo con el contrato y armo la respuesta.\n\n"
    "## Hallazgos del contraste\n\n- La cláusula 12 letra g dice que el proveedor asume la infraestructura.\n"
    "- Conviene una reunión con Andrés antes de responder.\n\n"
    "## Borrador de respuesta a Pablo\n\n**Asunto:** RE: Enlace de fibra óptica - Sede Norte\n\n"
    "Estimado Pablo, revisamos el anexo y el requerimiento es del proveedor.\n\n¿Quieres que ajuste el tono?"
)
MENSAJE = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: Word (.docx). No cambies a otro formato.]\n"
    "[Archivos adjuntos, ya disponibles en tu workspace:\n- Clausulas_Expresas.docx (42 KB) -> uploads/c56f.docx]\n\n"
    "es un archivo word"
)
VISTA_REAL = (
    "📅 **Vista previa de la reunión** (esto es exactamente lo que se creará):\n- **Título:** Alineación\n"
    "- **Inicio:** jueves 19-11-2026 10:00 (hora de Chile)\n- **Fin:** jueves 19-11-2026 10:45\n"
    "- **Invitados:** ninguno\n\n¿La creo? Responde «sí» para confirmar."
)


class TestFalsoPositivoDeVistaDeCalendario(unittest.TestCase):
    def test_un_analisis_con_borrador_de_correo_y_la_palabra_reunion_no_es_una_vista_de_calendario(self):
        self.assertFalse(runner.vista_calendario_a_mano(MENSAJE, ANALISIS, [], 0))

    def test_control_negativo_una_vista_de_calendario_escrita_a_mano_sigue_detectandose(self):
        self.assertTrue(runner.vista_calendario_a_mano("agéndame una reunión mañana", VISTA_REAL, [], 0))

    def test_una_vista_de_calendario_con_sus_campos_se_detecta_aunque_el_pedido_no_lo_diga(self):
        self.assertTrue(runner.vista_calendario_a_mano("sí, prepáralo", VISTA_REAL, [], 0))


RESPUESTA_ZIP = (
    "El ZIP que me pasaste no llegó a tu espacio de trabajo. Solo existe la carpeta vacía uploads/anexo_descomprimido "
    "que creé al intentar descomprimirlo. No tengo el archivo en ningún lado del workspace, así que no puedo leer el "
    "anexo de contrato. Puedes subir el archivo ZIP como adjunto en un nuevo mensaje o confirmarme la ruta exacta."
)


def _historial():
    return [
        {"role": "user", "content": "este es el anexo"},
        {"role": "assistant", "content": RESPUESTA_ZIP},
        {"role": "user", "content": "es un archivo word"},
        {"role": "assistant", "content": "Vista previa de la reunión " + "x" * 200},
        {
            "role": "user",
            "content": "esta ya fue realizada. Lo que justifica es que las zonas tienen personal 100% empresa",
        },
    ]


class TestRespuestaRepetidaDeTurnosAnteriores(unittest.TestCase):
    def test_copiar_la_respuesta_de_tres_turnos_atras_se_detecta(self):
        mensajes = _historial()
        self.assertTrue(
            runner.respuesta_repetida_de_antes(
                "esta ya fue realizada. Lo que justifica es otra cosa", RESPUESTA_ZIP, mensajes, 4
            )
        )

    def test_una_variante_casi_identica_tambien(self):
        variante = RESPUESTA_ZIP.replace("El ZIP", "El archivo ZIP")
        self.assertTrue(runner.respuesta_repetida_de_antes("otro mensaje distinto", variante, _historial(), 4))

    def test_control_negativo_una_respuesta_nueva_no_se_marca(self):
        nueva = (
            "Entendido: la justificación es que las zonas donde se cambia la red tienen personal 100% Empresa. "
            "Con eso el contraste queda así: la cláusula 12 letra g pide que el proveedor asuma la infraestructura, "
            "pero si el personal es propio el argumento de costo cambia y conviene replantear el alcance con Andrés."
        )
        self.assertFalse(runner.respuesta_repetida_de_antes("esta ya fue realizada", nueva, _historial(), 4))

    def test_la_ultima_respuesta_no_cuenta_porque_puede_ser_un_seguimiento_legitimo(self):
        historial = [{"role": "user", "content": "x"}, {"role": "assistant", "content": RESPUESTA_ZIP}]
        self.assertFalse(runner.respuesta_repetida_de_antes("¿y entonces?", RESPUESTA_ZIP, historial, 2))

    def test_si_el_usuario_pide_repetirla_no_se_marca(self):
        self.assertFalse(runner.respuesta_repetida_de_antes("repítela otra vez", RESPUESTA_ZIP, _historial(), 4))

    def test_respuestas_cortas_no_se_comparan(self):
        corta = [{"role": "assistant", "content": "Listo."}, {"role": "user", "content": "x"}]
        self.assertFalse(runner.respuesta_repetida_de_antes("y", "Listo.", corta, 1))


class TestFalsosPositivosReales(unittest.TestCase):
    def test_detalle_de_correos_de_bruno_con_la_palabra_reunion(self):
        respuesta = (
            "Los **CRÍTICOS** que quedan en el informe son 3: **#1555**, **#1556** y **#1557**.\n\n"
            "**Asunto:** Planilla PSI — pide una reunión el viernes.\n\n¿Quieres que responda alguno?"
        )
        self.assertFalse(runner.vista_calendario_a_mano("detalle de los 2 criticos", respuesta, [], 0))

    def test_borrador_de_correo_de_ana(self):
        respuesta = (
            "Archivo descargado. Ahora te muestro el borrador de la respuesta.\n\n**Para:** jmora@contoso.cl\n"
            "**Asunto:** RE: Solicitud de evento\n\nHola Julia, adjunto el archivo.\n\n¿Lo envío?"
        )
        self.assertFalse(
            runner.vista_calendario_a_mano("quiero responder a este correo con el archivo", respuesta, [], 0)
        )


class TestBorradorConPedidoDeReunion(unittest.TestCase):
    PEDIDO = "Analiza el último correo y prepárame un borrador con su asunto. Dime si conviene hacer una reunión."
    RESPUESTA = (
        "## El correo\nEs una prueba de funcionamiento.\n\n## Borrador de respuesta\n- **Para:** a@x.cl\n"
        "- **Asunto:** Re: Prueba auditoría A-P1\n\nHola Laura, confirmo la recepción.\n\n"
        "## ¿Reunión?\nNo hace falta una reunión.\n\n¿Lo envío?"
    )

    def test_el_borrador_no_es_calendario_aunque_el_pedido_hable_de_reunion(self):
        self.assertFalse(runner.vista_calendario_a_mano(self.PEDIDO, self.RESPUESTA, [], 0))

    def test_control_negativo_con_campos_de_calendario_si_se_detecta(self):
        respuesta = "**Asunto:** Comité\n**Inicio:** 10:00\n**Fin:** 10:30\n\n¿La agendo así?"
        self.assertTrue(runner.vista_calendario_a_mano("agenda una reunión con Ana el lunes", respuesta, [], 0))
