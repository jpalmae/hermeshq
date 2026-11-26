import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ms365_mail_get import _load

MENSAJE = {
    "subject": "Ticket 4521: acceso USB",
    "from": {"emailAddress": {"address": "marcelo@example.com"}},
    "toRecipients": [{"emailAddress": {"address": "usuario@example.com"}}],
    "receivedDateTime": "2026-10-16T18:00:00Z",
    "hasAttachments": True,
}
ADJUNTOS = [
    {"id": "A1", "name": "logo.png", "size": 4096, "isInline": True, "contentType": "image/png"},
    {"id": "A2", "name": "captura-ticket.png", "size": 90_000, "isInline": True, "contentType": "image/png"},
    {"id": "A3", "name": "detalle.pdf", "size": 20_000, "isInline": False, "contentType": "application/pdf"},
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.llamadas = []

    def _graph(self, error_en=None):
        def graph(method, path, token, payload=None, **kwargs):
            self.llamadas.append((method, path, payload))
            if error_en and error_en in path:
                return {"error": "Sin permiso para acceder a este recurso (403)."}
            if path.startswith("/me/messages/M1/attachments"):
                return {"value": ADJUNTOS}
            if method == "GET" and path.startswith("/me/messages/M1?"):
                return MENSAJE
            if path.endswith("/createForward"):
                return {"id": "DRAFT1"}
            if path.endswith("/createReply") or path.endswith("/createReplyAll"):
                return {"id": "DRAFTR"}
            if method == "GET" and path.startswith("/me/messages/DRAFTR?"):
                return {"body": {"content": "<div>--- mensaje original citado ---</div>"}}
            return {}

        return graph

    def _ejecutar(self, funcion, args, tarea="tarea-2", previa=True, error_en=None):
        def correr(argumentos, actual):
            with (
                patch.object(self.mod, "_current_task_id", return_value=actual),
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": actual, "mensaje": "sí"})}),
            ):
                return json.loads(funcion(argumentos))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph(error_en)),
        ):
            if args.get("aprobado_por_el_usuario") and previa:
                correr({k: v for k, v in args.items() if k != "aprobado_por_el_usuario"}, "tarea-1")
            return correr(args, tarea)

    def _acciones(self):
        return [c for c in self.llamadas if c[0] in ("POST", "PATCH")]


class TestForward(_Base):
    def _datos(self, **extra):
        return {"message_id": "M1", "to": ["pedro@example.com"], "comment": "Para tu revisión", **extra}

    def test_the_preview_lists_what_travels_with_the_original_and_sends_nothing(self):
        r = self._ejecutar(self.mod._forward_email_tool, self._datos())
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        vista = r["vista_previa"]
        self.assertEqual(vista["asunto"], "RV: Ticket 4521: acceso USB")
        self.assertEqual(vista["para"], ["pedro@example.com"])
        nombres = [a["nombre"] for a in vista["adjuntos_e_imagenes_que_viajan"]]
        self.assertEqual(nombres, ["logo.png", "captura-ticket.png", "detalle.pdf"])
        self.assertEqual(self._acciones(), [])

    def test_with_a_real_approval_it_forwards_the_original_message(self):
        r = self._ejecutar(self.mod._forward_email_tool, self._datos(aprobado_por_el_usuario=True))
        self.assertTrue(r["success"], r)
        accion = self._acciones()[0]
        self.assertEqual(accion[1], "/me/messages/M1/createForward")
        self.assertEqual(accion[2]["toRecipients"][0]["emailAddress"]["address"], "pedro@example.com")
        self.assertEqual(accion[2]["comment"], "Para tu revisión")
        rutas = [ruta for _, ruta, _ in self.llamadas]
        self.assertIn("/me/messages/DRAFT1/send", rutas)
        parche = [carga for metodo, _, carga in self.llamadas if metodo == "PATCH"][0]
        self.assertTrue(parche["subject"].startswith("RV: "))

    def test_the_agent_cannot_approve_in_the_same_task_or_without_a_preview(self):
        mismo = self._ejecutar(self.mod._forward_email_tool, self._datos(aprobado_por_el_usuario=True), tarea="tarea-1")
        self.assertFalse(mismo["success"])
        self.mod._previews_path().unlink(missing_ok=True)
        sin = self._ejecutar(self.mod._forward_email_tool, self._datos(aprobado_por_el_usuario=True), previa=False)
        self.assertFalse(sin["success"])
        self.assertIn("No se envió", sin["error"])
        self.assertEqual(self._acciones(), [])

    def test_a_copy_goes_through_a_draft_and_explains_the_missing_permission(self):
        r = self._ejecutar(
            self.mod._forward_email_tool, self._datos(cc=["jefe@example.com"], aprobado_por_el_usuario=True)
        )
        self.assertTrue(r["success"], r)
        rutas = [(m, p) for m, p, _ in self._acciones()]
        self.assertEqual(
            rutas,
            [
                ("POST", "/me/messages/M1/createForward"),
                ("PATCH", "/me/messages/DRAFT1"),
                ("POST", "/me/messages/DRAFT1/send"),
            ],
        )
        self.llamadas.clear()
        bloqueado = self._ejecutar(
            self.mod._forward_email_tool,
            self._datos(cc=["jefe@example.com"], comment="Otra revisión", aprobado_por_el_usuario=True),
            error_en="createForward",
        )
        self.assertFalse(bloqueado["success"])
        self.assertIn("Mail.ReadWrite", bloqueado["error"])

    def test_the_approval_is_used_once(self):
        datos = self._datos(aprobado_por_el_usuario=True)
        self.assertTrue(self._ejecutar(self.mod._forward_email_tool, datos)["success"])
        segundo = self._ejecutar(self.mod._forward_email_tool, datos, previa=False)
        self.assertFalse(segundo["success"])

    def test_bad_input_is_reported(self):
        self.assertIn("Se requiere", self._ejecutar(self.mod._forward_email_tool, {"message_id": "M1"})["error"])
        r = self._ejecutar(self.mod._forward_email_tool, {"message_id": "M1", "to": ["Pedro"]})
        self.assertIn("ms365_find_person", r["error"])


class TestReply(_Base):
    def test_reply_all_needs_the_same_real_approval(self):
        datos = {"message_id": "M1", "comment": "Gracias, lo reviso", "reply_all": True}
        vista = self._ejecutar(self.mod._reply_email_tool, datos)
        self.assertTrue(vista["requiere_aprobacion"])
        self.assertEqual(vista["vista_previa"]["accion"], "responder a todos")
        self.assertEqual(self._acciones(), [])
        r = self._ejecutar(self.mod._reply_email_tool, {**datos, "aprobado_por_el_usuario": True})
        self.assertTrue(r["success"], r)
        self.assertEqual(self._acciones()[0][1], "/me/messages/M1/replyAll")
        self.assertEqual(self._acciones()[0][2], {"comment": "Gracias, lo reviso"})

    def test_a_single_reply_uses_the_reply_action(self):
        datos = {"message_id": "M1", "comment": "Recibido", "aprobado_por_el_usuario": True}
        self.assertTrue(self._ejecutar(self.mod._reply_email_tool, datos)["success"])
        self.assertEqual(self._acciones()[0][1], "/me/messages/M1/reply")


class TestReplyConFirma(_Base):
    def test_reply_with_body_uses_createReply_patch_and_send(self):
        datos = {"message_id": "M1", "body": "<p>Hola Ana</p>", "aprobado_por_el_usuario": True}
        r = self._ejecutar(self.mod._reply_email_tool, datos)
        self.assertTrue(r["success"], r)
        rutas = [(m, p) for m, p, _ in self.llamadas]
        self.assertIn(("POST", "/me/messages/M1/createReply"), rutas)
        self.assertIn(("POST", "/me/messages/DRAFTR/send"), rutas)
        parche = next(pl for m, p, pl in self.llamadas if m == "PATCH" and p == "/me/messages/DRAFTR")
        self.assertIn("<p>Hola Ana</p>", parche["body"]["content"])
        self.assertIn("citado", parche["body"]["content"])

    def test_reply_all_with_body_uses_createReplyAll(self):
        datos = {"message_id": "M1", "body": "<p>Hola</p>", "reply_all": True, "aprobado_por_el_usuario": True}
        self.assertTrue(self._ejecutar(self.mod._reply_email_tool, datos)["success"])
        self.assertTrue(any(p.endswith("/createReplyAll") for _, p, _ in self.llamadas))

    def test_body_reworded_between_turns_needs_a_new_preview(self):
        def correr(a, actual):
            with (
                patch.object(self.mod, "_current_task_id", return_value=actual),
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": actual, "mensaje": "sí"})}),
            ):
                return json.loads(self.mod._reply_email_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph()),
        ):
            previo = correr({"message_id": "M1", "body": "<p>Hola</p>"}, "tarea-1")
            aprob = correr(
                {"message_id": "M1", "body": "<p>Estimada Ana, buenas tardes</p>", "aprobado_por_el_usuario": True},
                "tarea-2",
            )
        self.assertTrue(previo["requiere_aprobacion"])
        self.assertFalse(aprob["success"], aprob)
        self.assertTrue(aprob["requiere_aprobacion"])
        self.assertFalse(any(p.endswith("/send") for _, p, _ in self.llamadas))

    def test_reply_needs_comment_or_body(self):
        r = self._ejecutar(self.mod._reply_email_tool, {"message_id": "M1"})
        self.assertFalse(r["success"])
        self.assertIn("comment o body", r["error"])


class TestAttachments(_Base):
    def _descargar(self, args):
        def fake_bytes(path, token):
            return (b"datos-" + path.encode(), "") if "A3" in path or "A2" in path or "A1" in path else (None, "x")

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph()),
            patch.object(self.mod, "_graph_get_bytes", side_effect=fake_bytes),
        ):
            return json.loads(self.mod._mail_attachments_tool(args))

    def test_it_lists_attachments_and_inline_images(self):
        r = self._descargar({"message_id": "M1"})
        self.assertEqual(r["count"], 3)
        self.assertEqual([a["incrustado"] for a in r["adjuntos"]], [True, True, False])
        self.assertNotIn("descargados", r)

    def test_download_skips_inline_images_unless_asked(self):
        r = self._descargar({"message_id": "M1", "download": True})
        self.assertEqual([d["nombre"] for d in r["descargados"]], ["detalle.pdf"])
        self.assertEqual(len(r["omitidos"]), 2)
        self.assertTrue((Path(self.tmp.name) / r["descargados"][0]["local_path"]).is_file())
        todo = self._descargar({"message_id": "M1", "download": True, "include_inline": True})
        self.assertEqual(
            sorted(d["nombre"] for d in todo["descargados"]), ["captura-ticket.png", "detalle.pdf", "logo.png"]
        )

    def test_the_tools_are_registered_and_listed_in_the_manifest(self):
        class Ctx:
            def __init__(self):
                self.nombres = []

            def register_tool(self, **kwargs):
                self.nombres.append(kwargs["name"])

        ctx = Ctx()
        self.mod.register(ctx)
        manifiesto = (
            Path(__file__).resolve().parents[1]
            / "hermeshq"
            / "integration_packages"
            / "ms365-mail"
            / "plugin"
            / "plugin.yaml"
        ).read_text(encoding="utf-8")
        for nombre in ("ms365_mail_forward", "ms365_mail_reply", "ms365_mail_attachments"):
            self.assertIn(nombre, ctx.nombres)
            self.assertIn(nombre, manifiesto)


if __name__ == "__main__":
    unittest.main()


class TestReenvioAprobadoPorLaPlataforma(_Base):
    DATOS = {"message_id": "M1", "to": ["pedro@example.com"], "comment": "Para tu revisión"}
    MOSTRADO = (
        "Vista previa del reenvío:\n- Asunto: RV: Ticket 4521: acceso USB\n- Para: pedro@example.com\n"
        "- Comentario: Para tu revisión\n¿Lo reenvío?"
    )

    def _vista(self):
        r = self._ejecutar(self.mod._forward_email_tool, dict(self.DATOS), tarea="tarea-1")
        self.assertTrue(r["requiere_aprobacion"])

    def _aprobar(self, mensaje, anterior):
        payload = {"task_id": "tarea-2", "mensaje": mensaje, "respuesta_anterior": anterior}
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph()),
            patch.object(self.mod, "_current_task_id", return_value="tarea-2"),
            patch.object(self.mod, "_conversacion_actual", return_value=""),
            patch.object(self.mod, "_payload_tarea", return_value=payload),
            patch.object(self.mod, "es_aprobacion_pura", return_value=mensaje.strip().lower() == "sí"),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
        ):
            return self.mod.ejecutar_aprobacion_pendiente()

    def test_la_vista_previa_del_reenvio_queda_pendiente(self):
        with patch.object(self.mod, "_conversacion_actual", return_value=""):
            self._vista()
        pendiente = self.mod._leer_pendiente()
        self.assertEqual(pendiente["herramienta"], "forward")
        self.assertEqual(pendiente["args"]["message_id"], "M1")

    def test_el_si_reenvia_el_correo_de_la_vista_sin_el_modelo(self):
        with patch.object(self.mod, "_conversacion_actual", return_value=""):
            self._vista()
        envio = self._aprobar("sí", self.MOSTRADO)
        self.assertTrue(envio["resultado"]["success"], envio)
        self.assertEqual(envio["herramienta"], "ms365_mail_forward")
        self.assertIn("/me/messages/M1/createForward", [ruta for _, ruta, _ in self.llamadas])

    def test_un_si_con_cambios_no_reenvia(self):
        with patch.object(self.mod, "_conversacion_actual", return_value=""):
            self._vista()
        self.assertIsNone(self._aprobar("sí pero agrega a Juan", self.MOSTRADO))
        self.assertNotIn("/me/messages/M1/createForward", [ruta for _, ruta, _ in self.llamadas])

    def test_la_confirmacion_dice_reenviado(self):
        import hermeshq.scripts.hermes_task_runner as runner

        texto = runner._texto_envio(
            {"herramienta": "ms365_mail_forward", "preview": {"para": ["pedro@example.com"], "asunto": "RV: x"}}
        )
        self.assertIn("✅ Correo reenviado", texto)


class TestResponderUnMensajePropio(_Base):
    PROPIO = {
        "subject": "Re: Informe trimestral",
        "from": {"emailAddress": {"address": "asoto@contoso.cl"}},
        "toRecipients": [{"emailAddress": {"address": "equipo-datos@contoso.cl"}}],
        "receivedDateTime": "2026-11-20T13:58:00Z",
    }

    def _graph(self, error_en=None):
        base = super()._graph(error_en)

        def graph(method, path, token, payload=None, **kwargs):
            if path.startswith("/me?"):
                return {"mail": "asoto@contoso.cl", "userPrincipalName": "asoto@contoso.cl"}
            if method == "GET" and path.startswith("/me/messages/MP?"):
                return self.PROPIO
            return base(method, path, token, payload, **kwargs)

        return graph

    def test_responder_a_un_mensaje_propio_se_rechaza_y_no_envia_nada(self):
        r = self._ejecutar(self.mod._reply_email_tool, {"message_id": "MP", "comment": "Hola"})
        self.assertFalse(r["success"])
        self.assertIn("lo enviaste tú", r["error"])
        self.assertIn("equipo-datos@contoso.cl", r["error"])
        self.assertIn("reply_all=true", r["error"])
        self.assertEqual(self._acciones(), [])

    def test_con_reply_all_sobre_un_mensaje_propio_se_permite_y_va_a_los_destinatarios_originales(self):
        r = self._ejecutar(self.mod._reply_email_tool, {"message_id": "MP", "comment": "Hola", "reply_all": True})
        self.assertFalse(r["success"])
        self.assertTrue(r.get("requiere_aprobacion"), r)
        vista = r["vista_previa"]
        self.assertEqual(vista["responde_a"], "equipo-datos@contoso.cl")
        self.assertNotIn("asoto@contoso.cl", json.dumps(vista).lower())

    def test_control_negativo_un_mensaje_de_otra_persona_se_responde_normal(self):
        r = self._ejecutar(self.mod._reply_email_tool, {"message_id": "M1", "comment": "Recibido"})
        self.assertTrue(r.get("requiere_aprobacion"), r)
        self.assertEqual(r["vista_previa"]["responde_a"], "marcelo@example.com")
