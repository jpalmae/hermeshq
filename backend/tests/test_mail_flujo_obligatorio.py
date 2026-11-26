import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_mail_flujo_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

BORRADOR = (
    "Tienes razón, corregido.\n\n**Para:** equipo-datos@contoso.cl (Equipo Datos)\n**Asunto:** prueba\n\n"
    "> ¡Hola! muy buen día.\n\n¿Confirmas que lo envíe?"
)
PREVIEW = {"accion": "enviar correo", "para": ["equipo-datos@contoso.cl"], "cc": [], "cco": [], "asunto": "prueba"}


class _ConEntorno(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(plugin, "_modo_automatico", return_value=False).start()

    def entorno(self, mensaje, respuesta_anterior=""):
        payload = {"task_id": "T-actual", "metadata": {}, "mensaje": mensaje, "respuesta_anterior": respuesta_anterior}
        patch.dict(
            os.environ, {"HERMES_SESSION_CWD": self._tmp.name, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}
        ).start()


class TestBorradorMostradoValeComoVistaPrevia(_ConEntorno):
    def test_enviar_tras_un_borrador_con_destinatario_y_asunto_envia_a_la_primera(self):
        self.entorno("enviar", BORRADOR)
        self.assertIsNone(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", PREVIEW))

    def test_un_si_corto_tambien_aprueba(self):
        self.entorno("sí", BORRADOR)
        self.assertIsNone(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", PREVIEW))

    def test_otro_destinatario_que_no_se_mostro_pide_vista_previa(self):
        self.entorno("enviar", BORRADOR)
        otro = dict(PREVIEW, cc=["jefe@contoso.cl"])
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", otro))
        self.assertTrue(gate["requiere_aprobacion"])

    def test_otro_asunto_pide_vista_previa(self):
        self.entorno("enviar", BORRADOR)
        otro = dict(PREVIEW, asunto="Reunión de cierre")
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", otro))
        self.assertTrue(gate["requiere_aprobacion"])

    def test_si_el_usuario_no_aprueba_no_se_envia(self):
        self.entorno("cambia el saludo", BORRADOR)
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", PREVIEW))
        self.assertTrue(gate["requiere_aprobacion"])

    def test_una_pregunta_no_aprueba(self):
        self.entorno("sí, pero ¿lleva firma?", BORRADOR)
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", PREVIEW))
        self.assertTrue(gate["requiere_aprobacion"])

    def test_sin_mensaje_anterior_pide_vista_previa(self):
        self.entorno("enviar", "")
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", PREVIEW))
        self.assertTrue(gate["requiere_aprobacion"])


class TestFirmaSiempre(_ConEntorno):
    def _manifiesto(self, contenido):
        ruta = Path(self._tmp.name) / "work" / "firma"
        ruta.mkdir(parents=True)
        (ruta / "manifest.json").write_text(json.dumps(contenido), encoding="utf-8")

    def test_sin_firma_pedida_usa_la_personal(self):
        self.entorno("")
        self._manifiesto({"personal": {"imagenes": []}, "equipo": {"imagenes": []}})
        self.assertEqual(plugin._nombre_firma({}), "personal")

    def test_firma_pedida_se_respeta(self):
        self.entorno("")
        self._manifiesto({"personal": {"imagenes": []}, "equipo": {"imagenes": []}})
        self.assertEqual(plugin._nombre_firma({"firma": "equipo"}), "equipo")

    def test_sin_firma_explicito(self):
        self.entorno("")
        self._manifiesto({"personal": {"imagenes": []}})
        self.assertIsNone(plugin._nombre_firma({"firma": "ninguna"}))

    def test_si_el_usuario_escribio_sin_firma_no_se_pone_la_personal_aunque_el_modelo_no_lo_pida(self):
        self.entorno("Respóndelo con este texto. Sin firma. Muéstrame primero la vista previa.")
        self._manifiesto({"personal": {"imagenes": []}})
        self.assertIsNone(plugin._nombre_firma({}))

    def test_otras_frases_de_sin_firma(self):
        self._manifiesto({"personal": {"imagenes": []}})
        for frase in ("sin firma", "no uses firma", "con firma ninguna", "no pongas mi firma"):
            with self.subTest(frase=frase):
                self.entorno(f"envía el correo {frase}")
                self.assertIsNone(plugin._nombre_firma({}))

    def test_mencionar_la_firma_sin_negarla_sigue_usando_la_personal(self):
        self.entorno("envía el correo con mi firma y la de equipo")
        self._manifiesto({"personal": {"imagenes": []}})
        self.assertEqual(plugin._nombre_firma({}), "personal")

    def test_sin_manifiesto_no_hay_firma(self):
        self.entorno("")
        self.assertIsNone(plugin._nombre_firma({}))


class TestCuerpoTextoAHtml(unittest.TestCase):
    def test_parrafos_y_saltos(self):
        self.assertEqual(
            plugin._texto_a_html("¡Hola!\n\nLínea uno\nLínea dos\n\nSaludos,"),
            "<p>¡Hola!</p><p>Línea uno<br>Línea dos</p><p>Saludos,</p>",
        )

    def test_html_no_se_toca(self):
        html = "<p>Hola</p><p>Saludos</p>"
        self.assertEqual(plugin._texto_a_html(html), html)

    def test_escapa_simbolos(self):
        self.assertEqual(plugin._texto_a_html("a < b & c"), "<p>a &lt; b &amp; c</p>")


class TestUltimaRespuestaDelRunner(unittest.TestCase):
    def test_toma_el_ultimo_mensaje_del_agente(self):
        import importlib

        runner = importlib.import_module("hermeshq.scripts.hermes_task_runner")
        historial = [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "borrador 1"},
            {"role": "user", "content": "cambia"},
            {"role": "assistant", "content": [{"type": "text", "text": "borrador 2"}]},
        ]
        self.assertEqual(runner._ultima_respuesta(historial), "borrador 2")
        self.assertEqual(runner._ultima_respuesta(None), "")


class TestBorradorCanonico(unittest.TestCase):
    def test_formato_fijo_con_correos_parrafos_y_firma(self):
        cuerpo = plugin._texto_a_html("Hola,\n\nEs una prueba.\n\nSaludos,")
        self.assertEqual(
            plugin._borrador_canonico([("Para", "a@x.cl"), ("CC", ""), ("Asunto", "Prueba")], cuerpo, "personal"),
            "**Para:** a@x.cl\n**Asunto:** Prueba\n\n---\n\nHola,\n\nEs una prueba.\n\nSaludos,\n\n---\n\n"
            "_Firma: personal (la agrega el servidor al enviar)_",
        )

    def test_el_borrador_mostrado_se_reconoce_al_aprobar(self):
        borrador = plugin._borrador_canonico(
            [("Para", "equipo-datos@contoso.cl"), ("Asunto", "prueba")], "<p>Hola</p>", "personal"
        )
        payload = {
            "task_id": "T2",
            "metadata": {},
            "mensaje": "envía",
            "respuesta_anterior": borrador + "\n\n¿Lo envío?",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HERMES_SESSION_CWD": tmp, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_modo_automatico", return_value=False),
        ):
            self.assertIsNone(plugin._approval_gate({"aprobado_por_el_usuario": True}, "fp", dict(PREVIEW)))

    def test_la_respuesta_de_vista_previa_trae_el_borrador_y_la_instruccion(self):
        payload = {"task_id": "T1", "metadata": {}, "mensaje": "redacta", "respuesta_anterior": ""}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HERMES_SESSION_CWD": tmp, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_modo_automatico", return_value=False),
        ):
            gate = json.loads(plugin._approval_gate({}, "fp", dict(PREVIEW, borrador="**Para:** x")))
        self.assertEqual(gate["borrador"], "**Para:** x")
        self.assertIn("TAL CUAL", gate["siguiente"])
        self.assertNotIn("borrador", gate["vista_previa"])


class TestFirmaPegadaAMano(unittest.TestCase):
    def test_cid_sin_imagen_se_detecta(self):
        body = '<p>Saludos,</p><img src="cid:logo_firma"><img src="cid:logo">'
        self.assertEqual(plugin._cids_sin_imagen(body, [{"contentId": "logo"}]), ["logo_firma"])

    def test_cids_con_imagen_no_se_marcan(self):
        body = '<img src="cid:logo"><img src="cid:banner">'
        self.assertEqual(plugin._cids_sin_imagen(body, [{"contentId": "logo"}, {"contentId": "banner"}]), [])


class TestFirmaPegadaComoTexto(unittest.TestCase):
    FIRMA = (
        "<table><tr><td>Saluda cordialmente,</td></tr><tr><td><img src='cid:logo' alt='Logo Empresa'></td></tr>"
        "<tr><td>Bruno Castro<br>Reyes</td></tr><tr><td>Jefe de Área</td></tr>"
        "<tr><td>Sucursal: SUC1 (piso 3)<br>M: +56 9 1234 5678<br>E: mcastro@contoso.cl</td></tr></table>"
    )

    def test_firma_en_texto_copiada_se_corta(self):
        body = (
            "Buenas tardes, Nicolás:\n\nEs una prueba.\n\nSaludos,\n\nSaluda cordialmente,\n\n[Logo Empresa]\n\n"
            "Bruno Castro\nReyes\n\nJefe de Área\n\nSucursal: SUC1 (piso 3)\nM: +56 9 1234 5678\n"
            "E: mcastro@contoso.cl\n[Logo]\n[Banner] <https://www.contoso.cl>"
        )
        self.assertEqual(
            plugin._quitar_firma_pegada(body, self.FIRMA), "Buenas tardes, Nicolás:\n\nEs una prueba.\n\nSaludos,"
        )

    def test_cuerpo_normal_no_se_toca(self):
        body = "Hola Bruno Castro,\n\nTe escribo por la reunión.\n\nSaludos,"
        self.assertEqual(plugin._quitar_firma_pegada(body, self.FIRMA), body)

    def test_sin_firma_no_se_toca(self):
        self.assertEqual(plugin._quitar_firma_pegada("a\nb", None), "a\nb")


class TestDespedidaRepetida(unittest.TestCase):
    FIRMA_CON_DESPEDIDA = "<p>Saluda cordialmente,</p><p>Bruno Castro</p>"
    FIRMA_SIN_DESPEDIDA = "<p>Ana Soto</p><p>Ingeniera</p>"

    def test_si_la_firma_trae_despedida_se_quita_la_del_cuerpo(self):
        self.assertEqual(
            plugin._quitar_despedida_repetida("Hola,\n\nTexto.\n\nSaludos,", self.FIRMA_CON_DESPEDIDA),
            "Hola,\n\nTexto.",
        )

    def test_tambien_en_html(self):
        self.assertEqual(
            plugin._quitar_despedida_repetida("<p>Texto.</p><p>Saludos cordiales,</p>", self.FIRMA_CON_DESPEDIDA),
            "<p>Texto.</p>",
        )

    def test_si_la_firma_no_trae_despedida_el_cuerpo_queda_igual(self):
        body = "Hola,\n\nQuedo atenta ante cualquier consulta.\n\nSaludos,"
        self.assertEqual(plugin._quitar_despedida_repetida(body, self.FIRMA_SIN_DESPEDIDA), body)

    def test_cuerpo_sin_despedida_no_cambia(self):
        self.assertEqual(
            plugin._quitar_despedida_repetida("Hola,\n\nTexto.", self.FIRMA_CON_DESPEDIDA), "Hola,\n\nTexto."
        )


class TestFirmaPegadaSinDespedidaEnLaFirma(unittest.TestCase):
    FIRMA_ANA = "<p>Ana Soto<br>Rivas</p><p>Ingeniera en Inteligencia de Datos</p><p>E: asoto@empresa.cl</p>"

    def test_la_despedida_pegada_con_la_firma_tambien_se_quita(self):
        body = (
            "Hola,\n\nPrueba.\n\nSaludos,\n\nSaluda cordialmente,\n\n[Logo Empresa]\n\nAna Soto\nRivas\n"
            "Ingeniera en Inteligencia de Datos\nE: asoto@contoso.cl\n\n[Logo]"
        )
        self.assertEqual(plugin._quitar_firma_pegada(body, self.FIRMA_ANA), "Hola,\n\nPrueba.\n\nSaludos,")

    def test_una_sola_despedida_se_conserva(self):
        body = "Hola,\n\nPrueba.\n\nSaludos,\n\n[Logo Empresa]\nAna Soto\nRivas\nE: asoto@contoso.cl"
        self.assertEqual(plugin._quitar_firma_pegada(body, self.FIRMA_ANA), "Hola,\n\nPrueba.\n\nSaludos,")

    def test_firma_pegada_en_html_tambien_se_quita(self):
        body = (
            "<p>Hola,</p>\n<p>Prueba.</p>\n<p>Saludos,</p>\n<p>Saluda cordialmente,<br>\n[Logo Empresa]<br>\n"
            "Ana Soto<br>\nRivas<br>\nIngeniera en Inteligencia de Datos<br>\nE: asoto@contoso.cl<br>\n[Logo]</p>"
        )
        self.assertEqual(plugin._quitar_firma_pegada(body, self.FIRMA_ANA), "<p>Hola,</p><p>Prueba.</p><p>Saludos,</p>")

    def test_html_sin_firma_pegada_no_se_toca(self):
        body = "<p>Hola <b>Ana Soto</b>,</p><p>Saludos,</p>"
        self.assertEqual(plugin._quitar_firma_pegada(body, self.FIRMA_ANA), body)


class TestAprobacionPura(unittest.TestCase):
    def test_aprobaciones_limpias(self):
        for texto in (
            "sí",
            "Si",
            "ok",
            "envía",
            "envíalo",
            "sí, envíalo así",
            "ok envíalo por favor",
            "dale",
            "Sí, envía",
        ):
            self.assertTrue(plugin.es_aprobacion_pura(texto), texto)

    def test_no_son_aprobacion(self):
        for texto in (
            "sí pero cambia el saludo",
            "no lo envíes",
            "no se envió, envíalo",
            "¿lo envías?",
            "envía a Juan también",
            "cambia el asunto",
            "",
        ):
            self.assertFalse(plugin.es_aprobacion_pura(texto), texto)

    def test_quita_el_bloque_de_respuesta_corta_aunque_traiga_corchetes(self):
        prompt = (
            "[Respuesta corta a tu último mensaje: «**Para:** a@x.cl > [Logo Empresa] ¿Lo envío?». "
            "Interpreta «sí» como respuesta a eso, no a temas anteriores.]\nsí"
        )
        self.assertTrue(plugin.es_aprobacion_pura(prompt))


class TestEnvioDirectoPorLaPlataforma(_ConEntorno):
    ARGS = {"to": ["equipo-datos@contoso.cl"], "subject": "prueba", "body": "Hola"}

    def _pendiente(self):
        self.entorno("redacta", "")
        patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "T1", "metadata": {}})}).start()
        plugin._guardar_pendiente("send", dict(self.ARGS, aprobado_por_el_usuario=True), PREVIEW)

    def test_si_a_un_borrador_mostrado_envia_sin_el_modelo(self):
        self._pendiente()
        self.entorno("sí", BORRADOR)
        with patch.object(plugin, "_send_email_tool", return_value=json.dumps({"success": True})) as enviar:
            envio = plugin.ejecutar_aprobacion_pendiente()
        self.assertTrue(envio["resultado"]["success"])
        self.assertEqual(envio["herramienta"], "ms365_mail_send")
        self.assertEqual(enviar.call_args.args[0], dict(self.ARGS, aprobado_por_el_usuario=True))

    def test_tras_el_aviso_de_no_enviado_tambien_envia(self):
        self._pendiente()
        self.entorno("envía", "⚠️ El correo NO se envió: el envío no llegó a ejecutarse.")
        with patch.object(plugin, "_send_email_tool", return_value=json.dumps({"success": True})):
            self.assertIsNotNone(plugin.ejecutar_aprobacion_pendiente())

    def test_un_cambio_no_envia(self):
        self._pendiente()
        self.entorno("sí pero cambia el saludo", BORRADOR)
        with patch.object(plugin, "_send_email_tool") as enviar:
            self.assertIsNone(plugin.ejecutar_aprobacion_pendiente())
        enviar.assert_not_called()

    def test_sin_borrador_mostrado_no_envia(self):
        self._pendiente()
        self.entorno("sí", "Hola, ¿en qué te ayudo?")
        with patch.object(plugin, "_send_email_tool") as enviar:
            self.assertIsNone(plugin.ejecutar_aprobacion_pendiente())
        enviar.assert_not_called()

    def test_sin_pendiente_no_envia(self):
        self.entorno("sí", BORRADOR)
        self.assertIsNone(plugin.ejecutar_aprobacion_pendiente())

    def test_enviado_borra_el_pendiente(self):
        self._pendiente()
        plugin._consume_approval("fp")
        self.assertIsNone(plugin._leer_pendiente())


class TestEnvioDirectoEnElRunner(unittest.TestCase):
    def test_arma_un_resultado_con_la_llamada_real(self):
        import importlib

        runner = importlib.import_module("hermeshq.scripts.hermes_task_runner")
        falso = type(
            "P",
            (),
            {
                "ejecutar_aprobacion_pendiente": staticmethod(
                    lambda: {
                        "herramienta": "ms365_mail_send",
                        "args": {"to": ["a@x.cl"], "aprobado_por_el_usuario": True},
                        "resultado": {"success": True, "recipients": 1},
                        "preview": {"para": ["a@x.cl"], "asunto": "Hola"},
                    }
                )
            },
        )
        payload = {"task_id": "T2", "prompt": "sí", "hermes_home": "/x", "metadata": {"source": "public_chat"}}
        with patch.object(runner, "_cargar_plugin_correo", return_value=falso):
            resultado = runner._envio_aprobado_directo(payload)
        self.assertEqual(resultado["event"], "result")
        self.assertIn("✅ Correo enviado", resultado["final_response"])
        self.assertIn("a@x.cl", resultado["final_response"])
        herramientas = [m for m in resultado["messages"] if m["role"] == "tool"]
        self.assertEqual(herramientas[0]["name"], "ms365_mail_send")

    def test_tareas_programadas_no_usan_el_atajo(self):
        import importlib

        runner = importlib.import_module("hermeshq.scripts.hermes_task_runner")
        payload = {
            "task_id": "T2",
            "prompt": "sí",
            "hermes_home": "/x",
            "metadata": {"source": "schedule", "scheduled": True},
        }
        with patch.object(runner, "_cargar_plugin_correo") as cargar:
            self.assertIsNone(runner._envio_aprobado_directo(payload))
        cargar.assert_not_called()

    def test_si_el_envio_falla_sigue_con_el_modelo(self):
        import importlib

        runner = importlib.import_module("hermeshq.scripts.hermes_task_runner")
        falso = type(
            "P",
            (),
            {
                "ejecutar_aprobacion_pendiente": staticmethod(
                    lambda: {"herramienta": "ms365_mail_send", "args": {}, "resultado": {"success": False}}
                )
            },
        )
        payload = {"task_id": "T2", "prompt": "sí", "hermes_home": "/x", "metadata": {"source": "public_chat"}}
        with patch.object(runner, "_cargar_plugin_correo", return_value=falso):
            self.assertIsNone(runner._envio_aprobado_directo(payload))


BORRADOR_A_MANO = (
    "**Para:** admin@proveedor.com\n**Asunto:** Prueba firma pegada 8\n\n---\n\nEstimada,\n\n"
    "Probamos que la firma no salga duplicada.\n\nQuedo atenta ante cualquier consulta.\n\nSaludos,\n\n---\n\n¿Lo envío?"
)


class TestBorradorEscritoAMano(_ConEntorno):
    def test_lee_destinatario_asunto_y_cuerpo(self):
        args = plugin.args_desde_borrador_escrito(BORRADOR_A_MANO)
        self.assertEqual(args["to"], ["admin@proveedor.com"])
        self.assertEqual(args["subject"], "Prueba firma pegada 8")
        self.assertTrue(args["body"].startswith("Estimada,\n\nProbamos"))
        self.assertTrue(args["body"].endswith("Saludos,"))
        self.assertNotIn("firma", args)

    def test_lee_cc_y_sin_firma(self):
        texto = BORRADOR_A_MANO.replace("**Asunto:**", "**CC:** a@x.cl, b@x.cl\n**Asunto:**").replace(
            "¿Lo envío?", "_Sin firma_\n\n¿Lo envío?"
        )
        args = plugin.args_desde_borrador_escrito(texto)
        self.assertEqual(args["cc"], ["a@x.cl", "b@x.cl"])
        self.assertEqual(args["firma"], "ninguna")

    def test_respuestas_adjuntos_o_texto_suelto_no_se_leen(self):
        self.assertIsNone(
            plugin.args_desde_borrador_escrito(BORRADOR_A_MANO.replace("Prueba firma", "RE: Prueba firma"))
        )
        self.assertIsNone(
            plugin.args_desde_borrador_escrito(
                BORRADOR_A_MANO.replace("**Asunto:**", "**Adjuntos:** a.pdf\n**Asunto:**")
            )
        )
        self.assertIsNone(plugin.args_desde_borrador_escrito("Hola, ¿en qué te ayudo?"))

    def test_caso_real_bateria_c4_borrador_en_lista_a_mi_mismo(self):
        texto = (
            "📧 **Vista previa del borrador:**\n- **Para:** a mí mismo\n- **Asunto:** Batería C4 ciclo 3\n"
            "- **Cuerpo:** prueba de envío al propio buzón\n\n¿Lo envío? Responde «sí» para confirmar."
        )
        args = plugin.args_desde_borrador_escrito(texto)
        self.assertEqual(
            args, {"to": ["yo"], "subject": "Batería C4 ciclo 3", "body": "prueba de envío al propio buzón"}
        )

    def test_caso_real_lote16_para_a_mi_mismo_con_parentesis(self):
        texto = (
            "📧 **Vista previa del correo** (borrador, aún no enviado):\n- **Para:** a mí mismo (tu buzón)\n"
            "- **Asunto:** Batería L16 C4\n- **Cuerpo:** prueba de envío al propio buzón, lote 16\n\n"
            "¿Lo envío? Responde «sí» para confirmar."
        )
        self.assertEqual(plugin.args_desde_borrador_escrito(texto)["to"], ["yo"])
        for valor in ("a ti mismo", "tu buzón", "tu correo.", "Mí mismo", "a mí"):
            self.assertEqual(plugin._destinatarios_escritos(valor), ["yo"], valor)
        for valor in ("Juan", "el equipo de ventas", "tu jefe", "mi jefe (Pedro)"):
            self.assertEqual(plugin._destinatarios_escritos(valor), [], valor)

    def test_borrador_en_lista_con_cuerpo_de_varios_parrafos(self):
        texto = (
            "**Para:** ana@contoso.cl\n**CC:** mi correo\n**Asunto:** Reunión del lunes\n**Cuerpo:**\nHola Ana,\n\n"
            "Confirmo la reunión del lunes.\n\nSaludos,\n\n¿Lo envío?"
        )
        args = plugin.args_desde_borrador_escrito(texto)
        self.assertEqual(args["to"], ["ana@contoso.cl"])
        self.assertEqual(args["cc"], ["yo"])
        self.assertEqual(args["body"], "Hola Ana,\n\nConfirmo la reunión del lunes.\n\nSaludos,")

    def test_listas_que_no_son_un_correo_nuevo_no_se_leen(self):
        for texto in (
            "- **Asunto:** Batería K\n- **Inicio:** jueves 15:00\n¿La creo?",
            "- **Para:** alguien\n- **Asunto:** x\n- **Cuerpo:** hola\n¿Lo envío?",
            "- **Para:** ana@x.cl\n- **Asunto:** RE: Factura\n- **Cuerpo:** recibida\n¿Lo envío?",
            "- **Para:** ana@x.cl\n- **Adjuntos:** a.pdf\n- **Asunto:** Factura\n- **Cuerpo:** va\n¿Lo envío?",
            "**Responde a:** Juan\n- **Para:** juan@x.cl\n- **Asunto:** Factura\n- **Cuerpo:** ok\n¿Lo envío?",
            "- **Nombre:** Recordatorio\n- **Mensaje:** «Batería P1: revisar respaldos»\n¿Lo creo?",
        ):
            with self.subTest(texto=texto[:40]):
                self.assertIsNone(plugin.args_desde_borrador_escrito(texto))

    def test_la_vista_previa_nunca_envia_aunque_el_usuario_diga_si(self):
        self.entorno("sí", BORRADOR_A_MANO)
        llamadas = []

        def falso_envio(args):
            llamadas.append(dict(args))
            preview = {"para": args["to"], "asunto": args["subject"], "borrador": "BORRADOR"}
            gate = plugin._approval_gate(dict(args, aprobado_por_el_usuario=True), "fp-x", preview, "send")
            return gate if gate is not None else json.dumps({"success": True, "ENVIADO": True})

        with patch.object(plugin, "_send_email_tool", side_effect=falso_envio):
            vista = plugin.vista_previa_desde_borrador_escrito(BORRADOR_A_MANO)
        self.assertTrue(vista["resultado"]["requiere_aprobacion"])
        self.assertNotIn("ENVIADO", vista["resultado"])
        self.assertIsNotNone(plugin._leer_pendiente())

    def test_si_tras_borrador_a_mano_queda_listo_para_enviar(self):
        self.entorno("sí", BORRADOR_A_MANO)

        def falso_envio(args):
            preview = {"para": args["to"], "cc": [], "cco": [], "asunto": args["subject"], "borrador": "B"}
            gate = plugin._approval_gate(args, "fp-y", preview, "send")
            return gate if gate is not None else json.dumps({"success": True})

        with patch.object(plugin, "_send_email_tool", side_effect=falso_envio):
            self.assertIsNotNone(plugin.vista_previa_desde_borrador_escrito(BORRADOR_A_MANO, ya_mostrado=True))
            envio = plugin.ejecutar_aprobacion_pendiente()
        self.assertTrue(envio["resultado"]["success"])
        self.assertEqual(envio["args"]["to"], ["admin@proveedor.com"])


class TestBorradorAManoEnElRunner(unittest.TestCase):
    def _runner(self):
        import importlib

        return importlib.import_module("hermeshq.scripts.hermes_task_runner")

    def test_convierte_el_borrador_escrito_en_vista_previa(self):
        runner = self._runner()
        falso = type(
            "P",
            (),
            {
                "vista_previa_desde_borrador_escrito": staticmethod(
                    lambda texto, ya_mostrado=False: {
                        "herramienta": "ms365_mail_send",
                        "args": {"to": ["a@x.cl"]},
                        "resultado": {"success": False, "requiere_aprobacion": True, "borrador": "**Para:** a@x.cl"},
                    }
                )
            },
        )
        payload = {"task_id": "T3", "hermes_home": "/x", "metadata": {"source": "public_chat"}}
        mensajes = [{"role": "user", "content": "correo"}, {"role": "assistant", "content": BORRADOR_A_MANO}]
        with patch.object(runner, "_cargar_plugin_correo", return_value=falso):
            texto, nuevos = runner._borrador_a_vista_previa(payload, BORRADOR_A_MANO, mensajes)
        self.assertTrue(texto.startswith("**Para:** a@x.cl"))
        self.assertTrue(texto.endswith("¿Lo envío?"))
        self.assertTrue(any(m.get("role") == "tool" and m.get("name") == "ms365_mail_send" for m in nuevos))

    def test_no_convierte_si_ya_llamo_a_la_herramienta(self):
        runner = self._runner()
        payload = {"task_id": "T3", "hermes_home": "/x", "metadata": {"source": "public_chat"}}
        mensajes = [
            {"role": "user", "content": "correo"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "ms365_mail_send"}}]},
        ]
        with patch.object(runner, "_cargar_plugin_correo") as cargar:
            self.assertIsNone(runner._borrador_a_vista_previa(payload, BORRADOR_A_MANO, mensajes))
        cargar.assert_not_called()

    def test_busca_el_borrador_antes_del_aviso_de_no_enviado(self):
        runner = self._runner()
        historial = [
            {"role": "assistant", "content": BORRADOR_A_MANO},
            {"role": "user", "content": "sí"},
            {"role": "assistant", "content": "⚠️ El correo NO se envió: el envío no llegó a ejecutarse."},
        ]
        self.assertEqual(runner._borrador_escrito_anterior(historial), BORRADOR_A_MANO)
        self.assertEqual(runner._borrador_escrito_anterior(historial[:1]), BORRADOR_A_MANO)
