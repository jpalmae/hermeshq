import json
import unittest

from hermeshq.services import respuesta_verificada
from hermeshq.services.respuesta_verificada import (
    AVISO_SIN_ADJUNTO,
    con_aviso_de_adjunto,
    con_aviso_envio_no_confirmado,
    envios_confirmados,
    parrafos_sin_respaldo,
)


class TestAvisoDeAdjunto(unittest.TestCase):
    def test_te_adjunto_sin_media_agrega_el_aviso(self):
        texto = "Listo, te adjunto el DOC-PRO-0040_V3.docx"
        self.assertEqual(con_aviso_de_adjunto(texto), f"{texto}\n\n{AVISO_SIN_ADJUNTO}")

    def test_con_media_no_cambia(self):
        texto = "Listo, te adjunto el archivo.\nMEDIA: uploads/x.docx"
        self.assertEqual(con_aviso_de_adjunto(texto), texto)

    def test_sin_mencion_de_adjunto_no_cambia(self):
        texto = "Listo, el reporte está generado."
        self.assertEqual(con_aviso_de_adjunto(texto), texto)

    def test_vacio(self):
        self.assertIsNone(con_aviso_de_adjunto(None))
        self.assertEqual(con_aviso_de_adjunto(""), "")


class TestParrafosSinRespaldo(unittest.TestCase):
    FUENTE = ["Pauta 4S mecánica: revisar frenos y temperatura de reductores.", "Lubricación 12S: revisar niveles."]

    def test_detecta_el_parrafo_inventado(self):
        generados = [
            "Pauta 4S mecánica: revisar frenos y temperatura de reductores.",
            "Servicio externo ejecuta predictivo de endoscopia con tintas penetrantes en vigas cuadradas.",
        ]
        sin = parrafos_sin_respaldo(generados, self.FUENTE)
        self.assertEqual(len(sin), 1)
        self.assertIn("endoscopia", sin[0])

    def test_parrafos_cortos_no_se_juzgan(self):
        self.assertEqual(parrafos_sin_respaldo(["Sí."], self.FUENTE), [])

    def test_todo_respaldado_no_marca_nada(self):
        self.assertEqual(parrafos_sin_respaldo(["Lubricación 12S: revisar niveles."], self.FUENTE), [])


class AvisoEnvioNoConfirmado(unittest.TestCase):
    OK = {
        "role": "tool",
        "name": "ms365_mail_send",
        "content": '{"success": true, "message": "Correo enviado correctamente."}',
    }
    PREVIA = {"role": "tool", "name": "ms365_mail_send", "content": '{"success": false, "requiere_aprobacion": true}'}

    def test_afirma_envio_sin_herramienta_exitosa_se_marca(self):
        texto = "Correo enviado correctamente: Para: admin@proveedor.com"
        self.assertIn("No hay confirmación de envío", con_aviso_envio_no_confirmado(texto, []))

    def test_vista_previa_no_cuenta_como_envio(self):
        texto = "Correo enviado correctamente."
        self.assertIn("No hay confirmación de envío", con_aviso_envio_no_confirmado(texto, [self.PREVIA]))

    def test_envio_confirmado_no_se_marca(self):
        texto = "Correo enviado correctamente."
        self.assertEqual(con_aviso_envio_no_confirmado(texto, [self.OK]), texto)

    def test_texto_sin_afirmar_envio_no_cambia(self):
        texto = "Te muestro la vista previa del correo."
        self.assertEqual(con_aviso_envio_no_confirmado(texto, []), texto)

    def test_envios_confirmados_cuenta_solo_exitos(self):
        self.assertEqual(envios_confirmados([self.OK, self.PREVIA, {"role": "user", "content": "x"}]), 1)


class BorradorCanonico(unittest.TestCase):
    PREVIA = {
        "role": "tool",
        "name": "ms365_mail_send",
        "content": json.dumps(
            {
                "success": False,
                "requiere_aprobacion": True,
                "vista_previa": {
                    "verificacion_directorio": {"x@otro.cl": "⚠ NO está en el directorio (externo o inexistente)"}
                },
                "borrador": "**Para:** x@otro.cl\n**Asunto:** Hola\n\n> Hola\n\n_Firma: personal (la agrega el servidor al enviar)_",
            }
        ),
    }
    ENVIADO = {"role": "tool", "name": "ms365_mail_send", "content": json.dumps({"success": True, "recipients": 1})}

    def test_el_borrador_del_modelo_se_reemplaza_por_el_del_servidor(self):
        texto = respuesta_verificada.con_borrador_canonico("- Para: x\n- Asunto: Hola\n\n¿Lo envío?", [self.PREVIA])
        self.assertTrue(texto.startswith("**Para:** x@otro.cl\n**Asunto:** Hola"))
        self.assertIn("⚠️ Revisa estos destinatarios: x@otro.cl", texto)
        self.assertTrue(texto.endswith("¿Lo envío?"))

    def test_caso_real_6a5e088e_el_aviso_de_externos_sale_una_sola_vez(self):
        aviso = (
            "⚠️ Revisa estas direcciones antes de decir «sí»: no están en el directorio de la empresa (pueden ser "
            "externas o estar mal escritas): x@otro.cl"
        )
        resultado = json.loads(self.PREVIA["content"])
        resultado["borrador"] += "\n\n" + aviso
        resultado["advertencia_destinatarios"] = aviso
        previa = {**self.PREVIA, "content": json.dumps(resultado)}
        texto = respuesta_verificada.con_borrador_canonico("¿Lo envío?", [previa])
        self.assertEqual(texto.count("x@otro.cl: ⚠"), 0)
        self.assertEqual(texto.count(aviso), 1)
        self.assertTrue(texto.endswith("¿Lo envío?"))

    def test_si_despues_se_envio_no_se_toca(self):
        self.assertEqual(
            respuesta_verificada.con_borrador_canonico("Enviado.", [self.PREVIA, self.ENVIADO]), "Enviado."
        )

    def test_sin_vista_previa_no_se_toca(self):
        self.assertEqual(respuesta_verificada.con_borrador_canonico("Hola", []), "Hola")


class EnvioFalsoNoLlegaAlUsuario(unittest.TestCase):
    def test_si_el_usuario_pidio_enviar_la_afirmacion_falsa_se_reemplaza(self):
        texto = respuesta_verificada.con_aviso_envio_no_confirmado("Correo enviado a x@y.cl.", [], "sí, envíalo así")
        self.assertEqual(texto, respuesta_verificada.RESPUESTA_NO_ENVIADO)
        self.assertNotIn("Correo enviado", texto)

    def test_con_el_bloque_de_respuesta_corta(self):
        prompt = "[Respuesta corta a tu último mensaje: «Para: x». Interpreta «sí» como respuesta a eso, no a temas anteriores.]\nsí"
        self.assertEqual(
            respuesta_verificada.con_aviso_envio_no_confirmado("Listo, enviado.", [], prompt),
            respuesta_verificada.RESPUESTA_NO_ENVIADO,
        )

    def test_otras_formas_de_afirmar_envio(self):
        for frase in ("El correo fue enviado.", "Ya lo envié.", "Se envió correctamente.", "Correo enviado con éxito."):
            self.assertEqual(
                respuesta_verificada.con_aviso_envio_no_confirmado(frase, [], "envía"),
                respuesta_verificada.RESPUESTA_NO_ENVIADO,
                frase,
            )

    def test_una_pregunta_del_usuario_solo_agrega_el_aviso(self):
        texto = respuesta_verificada.con_aviso_envio_no_confirmado(
            "Sí, el correo enviado ayer llegó.", [], "¿se mandó?"
        )
        self.assertTrue(texto.startswith("Sí, el correo enviado ayer llegó."))

    def test_con_envio_real_no_se_toca(self):
        enviado = [{"role": "tool", "name": "ms365_mail_send", "content": json.dumps({"success": True})}]
        self.assertEqual(
            respuesta_verificada.con_aviso_envio_no_confirmado("Correo enviado.", enviado, "sí"), "Correo enviado."
        )


class TestTablasDeCorreoSiempreDelServidor(unittest.TestCase):
    TABLA = "| ID | Edad | Asunto | Remitente | Relación | Acción requerida | Plazo | Respuesta |\n|---|---|---|---|---|---|---|---|\n| #521 | 48 min | OC | Compras | Para mí | Aprobar | Hoy | Sí |"

    def _mensajes(self):
        import json as _json

        tablas = {
            "INMEDIATO": "_(sin correos nuevos en esta ventana)_",
            "CRÍTICO": self.TABLA,
            "CALIENTE": "_(sin correos nuevos en esta ventana)_",
            "TIBIO": "_(sin correos nuevos en esta ventana)_",
            "FRÍO": "_(sin correos nuevos en esta ventana)_",
        }
        return [
            {
                "role": "tool",
                "name": "ms365_correo_sync",
                "content": _json.dumps({"success": True, "tablas_md": tablas}),
            }
        ]

    def test_columnas_viejas_se_reemplazan(self):
        from hermeshq.services.respuesta_verificada import con_tablas_de_correo

        texto = (
            "📥 Informe horario\n\n| Categoría | Cantidad |\n|---|---|\n| 🔴 CRÍTICO | 1 |\n\n"
            "🚨 INMEDIATO (acción ya, sin margen)\n_(sin correos nuevos en esta ventana)_\n\n"
            "🔴 CRÍTICO (pendiente de ti)\n| # | Asunto | Remitente | Acción | Plazo |\n|---|---|---|---|---|\n| #521 | OC | Compras | Aprobar | Hoy |\n\n"
            "⚪ FRÍOS CONSOLIDADOS\n0 correos.\n\nResumen del run: CRÍTICO hay uno."
        )
        salida = con_tablas_de_correo(texto, self._mensajes())
        self.assertIn(self.TABLA, salida)
        self.assertNotIn("| # | Asunto |", salida)
        self.assertIn("| 🔴 CRÍTICO | 1 |", salida)
        self.assertIn("⚪ FRÍOS CONSOLIDADOS\n0 correos.", salida)
        self.assertTrue(salida.rstrip().endswith("Resumen del run: CRÍTICO hay uno."))

    def test_bloques_por_correo_se_reemplazan(self):
        from hermeshq.services.respuesta_verificada import con_tablas_de_correo

        texto = "🔴 CRÍTICO\n- #521 OC — Compras\n- Acción: aprobar\n\n🟠 CALIENTE\n_(sin correos nuevos en esta ventana)_\n\nResumen."
        salida = con_tablas_de_correo(texto, self._mensajes())
        self.assertIn(self.TABLA, salida)
        self.assertNotIn("- Acción: aprobar", salida)

    def test_el_formato_correcto_queda_igual(self):
        from hermeshq.services.respuesta_verificada import con_tablas_de_correo

        texto = "🔴 CRÍTICO (pendiente de ti)\n" + self.TABLA + "\n\nResumen."
        self.assertEqual(con_tablas_de_correo(texto, self._mensajes()).count("| #521 |"), 1)

    def test_sin_informe_de_correo_no_toca_nada(self):
        from hermeshq.services.respuesta_verificada import con_tablas_de_correo

        texto = "🔴 CRÍTICO\n| a | b |"
        self.assertEqual(con_tablas_de_correo(texto, []), texto)


class TestEnvioPorTeamsConfirmado(unittest.TestCase):
    def _herramienta(self, nombre, resultado):
        return {"role": "tool", "name": nombre, "content": json.dumps(resultado)}

    def test_mensaje_de_teams_exitoso_confirma_el_envio(self):
        texto = "Mensaje enviado correctamente a Colega por Teams."
        mensajes = [self._herramienta("teams_send_to_person", {"success": True, "id": "msg-1"})]
        self.assertEqual(respuesta_verificada.con_aviso_envio_no_confirmado(texto, mensajes, "sí, hazlo"), texto)

    def test_teams_que_solo_pidio_aprobacion_no_confirma(self):
        texto = "Mensaje enviado correctamente."
        mensajes = [self._herramienta("teams_send_chat_message", {"success": False, "requiere_aprobacion": True})]
        self.assertNotEqual(respuesta_verificada.con_aviso_envio_no_confirmado(texto, mensajes, "envíalo"), texto)


class TestAccionGraphNoConfirmada(unittest.TestCase):
    def _tool(self, nombre, cuerpo):
        return {"role": "tool", "name": nombre, "content": json.dumps(cuerpo)}

    def test_afirmar_reunion_creada_sin_herramienta_agrega_aviso(self):
        texto = "He creado la reunión para el martes a las 15:00."
        resultado = respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, [])
        self.assertNotEqual(resultado, texto)
        self.assertIn("No hay confirmación en Microsoft 365", resultado)

    def test_reunion_con_exito_del_servidor_no_agrega_aviso(self):
        texto = "He creado la reunión para el martes a las 15:00."
        mensajes = [self._tool("ms365_calendar_create_event", {"success": True, "event": {"id": "evt-1"}})]
        self.assertEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, mensajes), texto)

    def test_solo_pedir_aprobacion_no_confirma_nada(self):
        texto = "Listo, compartido en la organización."
        mensajes = [self._tool("sharepoint_share_file", {"success": False, "requiere_aprobacion": True})]
        self.assertNotEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, mensajes), texto)

    def test_repeticion_con_resultado_registrado_cuenta_como_confirmada(self):
        texto = "El enlace quedó compartido."
        mensajes = [
            self._tool("sharepoint_share_file", {"success": True, "ya_ejecutada": True, "resultado": {"link": {}}})
        ]
        self.assertEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, mensajes), texto)

    def test_resultado_desconocido_no_cuenta_como_confirmado(self):
        texto = "Mensaje enviado en Teams."
        mensajes = [self._tool("ms365_calendar_respond_event", {"success": False, "estado": "desconocido"})]
        self.assertNotEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, mensajes), texto)

    def test_texto_sin_afirmacion_de_accion_no_cambia(self):
        texto = "Te resumo el documento en tres puntos."
        self.assertEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, []), texto)


class VistaPreviaDeCalendario(unittest.TestCase):
    VISTA = (
        "📅 **Vista previa de la reunión** (esto es exactamente lo que se creará):\n- **Título:** Prueba R65 HermesHQ\n"
        "- **Inicio:** lunes 16-11-2026 18:00 (hora de Chile)\n- **Invitados:** ninguno\n"
        "- **Descripción:** Evento creado desde HermesHQ.\n\n¿La creo? Responde «sí» para confirmar."
    )
    PENDIENTE = {
        "role": "tool",
        "name": "ms365_calendar_create_event",
        "content": json.dumps({"success": False, "requiere_aprobacion": True, "vista_previa": {}}),
    }

    def test_caso_real_27b29f66_la_vista_previa_no_lleva_el_aviso(self):
        self.assertEqual(
            respuesta_verificada.con_aviso_accion_graph_no_confirmada(self.VISTA, [self.PENDIENTE]), self.VISTA
        )
        self.assertEqual(respuesta_verificada.con_aviso_accion_graph_no_confirmada(self.VISTA, []), self.VISTA)

    def test_afirmar_que_se_creo_sin_herramienta_sigue_avisando(self):
        texto = "Listo, reunión creada para el lunes a las 18:00."
        salida = respuesta_verificada.con_aviso_accion_graph_no_confirmada(texto, [])
        self.assertIn(respuesta_verificada.AVISO_ACCION_GRAPH_NO_CONFIRMADA, salida)
