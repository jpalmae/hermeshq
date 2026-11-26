import unittest

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO_TEAMS = (
    "prepara un mensaje por Teams para el chat con Víctor Ortiz que diga: prueba de nombre del chat lote 20b, no "
    "enviar. Solo muéstrame la vista previa"
)
RESPUESTA_TEAMS = (
    "Vista previa del mensaje para Teams (aún no enviado):\n\n- **Chat:** Víctor Ortiz\n- **Mensaje:** prueba de "
    "nombre del chat lote 20b, no enviar. Solo muéstrame la vista previa\n\n¿Confirmas que lo envíe?"
)
PEDIDO_CORREO = (
    "envíame un correo de prueba a mí mismo con asunto «Batería L16 C4» y el texto: prueba de envío al propio buzón, "
    "lote 16"
)
RESPUESTA_CORREO = (
    "📧 **Vista previa del correo** (borrador, aún no enviado):\n- **Para:** a mí mismo (tu buzón)\n- **Asunto:** "
    "Batería L16 C4\n- **Cuerpo:** prueba de envío al propio buzón, lote 16\n\n¿Lo envío? Responde «sí» para confirmar."
)


def _turno(pedido, *herramientas):
    mensajes = [{"role": "user", "content": pedido}]
    for i, nombre in enumerate(herramientas):
        mensajes += [
            {"role": "assistant", "tool_calls": [{"id": f"t{i}", "function": {"name": nombre}}]},
            {"role": "tool", "tool_call_id": f"t{i}", "name": nombre, "content": '{"success": true}'},
        ]
    return mensajes


class TestVistaEnvioAMano(unittest.TestCase):
    def test_caso_real_teams_sin_herramienta(self):
        self.assertEqual(runner.vista_envio_a_mano(PEDIDO_TEAMS, RESPUESTA_TEAMS, _turno(PEDIDO_TEAMS), 0), "teams")

    def test_caso_real_correo_sin_herramienta(self):
        self.assertEqual(runner.vista_envio_a_mano(PEDIDO_CORREO, RESPUESTA_CORREO, _turno(PEDIDO_CORREO), 0), "correo")

    def test_solo_lecturas_antes_tambien_cuenta(self):
        mensajes = _turno(PEDIDO_TEAMS, "teams_list_chats")
        self.assertEqual(runner.vista_envio_a_mano(PEDIDO_TEAMS, RESPUESTA_TEAMS, mensajes, 0), "teams")

    def test_con_la_herramienta_de_envio_no(self):
        for nombre in ("teams_send_chat_message", "teams_send_to_person"):
            mensajes = _turno(PEDIDO_TEAMS, nombre)
            self.assertIsNone(runner.vista_envio_a_mano(PEDIDO_TEAMS, RESPUESTA_TEAMS, mensajes, 0))
        mensajes = _turno(PEDIDO_CORREO, "ms365_find_person", "ms365_mail_send")
        self.assertIsNone(runner.vista_envio_a_mano(PEDIDO_CORREO, RESPUESTA_CORREO, mensajes, 0))

    def test_replay_pregunta_de_estado_no(self):
        pedido = "enviaste el mensaje de Teams?"
        respuesta = (
            "No, todavía no lo envié. Te mostré la vista previa y quedé a la espera de tu confirmación. ¿Te lo envío "
            "ahora? (Julia Mora — «Hola, esta es una prueba con HermesHQ»)"
        )
        self.assertIsNone(runner.vista_envio_a_mano(pedido, respuesta, _turno(pedido), 0))

    def test_replay_solo_redaccion_no(self):
        for pedido in (
            "mandame el resumen redactado tipo correo para enviarlo a cliente, indicando que se está monitoreando",
            "Redacta un correo a Bruno Castro confirmando la reunión del jueves a las 10. Usa la firma",
        ):
            with self.subTest(pedido=pedido[:40]):
                respuesta = "Aquí va el borrador:\n\n**Para:** Bruno\n**Asunto:** Reunión\n\n¿Quieres que lo envíe?"
                self.assertIsNone(runner.vista_envio_a_mano(pedido, respuesta, _turno(pedido), 0))

    def test_respuesta_que_no_es_vista_no(self):
        respuesta = "Hay dos chats con Tomás: «Víctor Ortiz» y «Tomás Pérez». ¿A cuál lo envío?"
        self.assertIsNone(runner.vista_envio_a_mano(PEDIDO_TEAMS, respuesta, _turno(PEDIDO_TEAMS), 0))

    def test_la_nota_nombra_la_herramienta_y_pide_el_texto_sin_instrucciones(self):
        nota = runner.nota_accion_narrada("vista_envio_teams", RESPUESTA_TEAMS)
        self.assertIn("teams_send_chat_message", nota)
        self.assertIn("chat_nombre", nota)
        self.assertIn("sin las instrucciones del usuario", nota)
        self.assertIn("ms365_mail_send", runner.nota_accion_narrada("vista_envio_correo", RESPUESTA_CORREO))
        self.assertIn("no enviaría nada", runner._AVISO_VISTA_ENVIO_SIN_REGISTRAR)
