import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermeshq.services.aprobacion_central import es_aprobacion_textual
from tests.test_calendar_teams_meeting import _load as _cargar_calendario
from tests.test_ms365_mail_get import _load as _cargar_correo
from tests.test_sharepoint_share_file import sp
from tests.test_teams_send_to_person import _cargar as _cargar_teams

HORA = (
    "\n\n[Hora actual: lunes 2026-11-10 10:00 America/Santiago (UTC-03:00) = 2026-11-10T13:00:00Z. "
    "No necesitas ejecutar código para saber la hora.]"
)


def _con_referencia(texto: str, referencia: str = "Para: ana@x.cl Asunto: Informe. No incluye adjuntos. ¿Lo envío?"):
    return (
        f"[Respuesta corta a tu último mensaje: «{referencia}». Interpreta «{texto}» como respuesta a eso, "
        f"no a temas anteriores.]\n{texto}{HORA}"
    )


NEGATIVOS = (
    "olvidalooo",
    "olvídalo",
    "no",
    "nooo",
    "mejor no",
    "espera",
    "cancela",
    "sí, pero espera",
    "si no",
    "no, gracias",
    "todavía no",
    "déjalo",
    "¿sí?",
)
AFIRMATIVOS = ("sí", "si, envialo", "Sí, envíalo", "ok", "dale", "sí, por favor", "confirmo")


class _Entorno(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name, "HERMESHQ_APROBACION_CENTRAL": "0"})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.llamadas = []

    @staticmethod
    def _turno(mod, tarea, mensaje, funcion, args, *parches):
        carga = json.dumps({"task_id": tarea, "metadata": {}, "mensaje": mensaje})
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
            patch.object(
                mod, "_current_task_id" if hasattr(mod, "_current_task_id") else "_task_id", return_value=tarea
            ),
        ):
            return json.loads(funcion(args))

    def _escrituras(self):
        return [c for c in self.llamadas if c[0] in ("POST", "PATCH", "DELETE", "PUT")]


class TestDetectorDeConfirmacion(unittest.TestCase):
    def _detectores(self):
        correo = _cargar_correo()
        calendario = _cargar_calendario()
        teams = _cargar_teams()
        return {
            "correo": lambda: correo._usuario_aprueba_ahora() or correo._aprobacion_en_el_mensaje(),
            "calendario": calendario._usuario_confirma,
            "teams": teams._usuario_confirma,
            "sharepoint": sp._usuario_confirma,
        }

    def _evaluar(self, detector, mensaje):
        carga = json.dumps({"task_id": "t2", "metadata": {}, "mensaje": mensaje})
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}):
            return detector()

    def test_negacion_o_cancelacion_nunca_confirma(self):
        for nombre, detector in self._detectores().items():
            for texto in NEGATIVOS:
                for mensaje in (texto, _con_referencia(texto)):
                    with self.subTest(plugin=nombre, mensaje=mensaje):
                        self.assertFalse(self._evaluar(detector, mensaje))

    def test_si_explicito_confirma_aunque_la_plataforma_agregue_referencia_y_hora(self):
        for nombre, detector in self._detectores().items():
            for texto in AFIRMATIVOS:
                for mensaje in (texto, _con_referencia(texto)):
                    with self.subTest(plugin=nombre, mensaje=mensaje):
                        self.assertTrue(self._evaluar(detector, mensaje))

    def test_sin_mensaje_del_usuario_no_hay_confirmacion(self):
        for nombre, detector in self._detectores().items():
            with self.subTest(plugin=nombre), patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({})}):
                self.assertFalse(detector())

    def test_coincide_con_el_si_por_texto_de_la_aprobacion_central(self):
        for texto in ("sí", "ok", "dale", "confirmo", "sí, por favor", "no", "olvidalooo", "mejor no", "¿sí?"):
            with self.subTest(texto=texto):
                carga = json.dumps({"mensaje": texto})
                with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}):
                    self.assertEqual(sp._usuario_confirma(), es_aprobacion_textual(texto))


class TestCandadoCorreo(_Entorno):
    def setUp(self):
        super().setUp()
        self.mod = _cargar_correo()
        patch.object(self.mod, "_modo_automatico", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _graph(self, method, path, token, payload=None, **_kw):
        self.llamadas.append((method, path, payload))
        if method == "GET" and path.startswith("/me/messages/M1"):
            return {
                "id": "M1",
                "subject": "Informe",
                "from": {"emailAddress": {"address": "ana@x.cl"}},
                "toRecipients": [],
                "receivedDateTime": "2026-11-10T10:00:00Z",
            }
        if method == "GET" and "/attachments" in path:
            return {"value": []}
        if method == "POST" and path.endswith("/createReply"):
            return {"id": "D1"}
        return {}

    def _dos_turnos(self, funcion, previa, aprobada, mensaje):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            primero = self._turno(self.mod, "tarea-1", "redacta el correo", funcion, previa)
            self.llamadas.clear()
            segundo = self._turno(self.mod, "tarea-2", mensaje, funcion, {**aprobada, "aprobado_por_el_usuario": True})
        self.assertTrue(primero["requiere_aprobacion"], primero)
        return segundo

    ENVIO = {"to": "ana@x.cl", "subject": "Informe", "body": "Hola Ana, va el informe."}

    def test_send_olvidalo_con_aprobado_true_no_envia(self):
        for mensaje in ("olvidalooo", _con_referencia("olvidalooo"), "mejor no", "no, espera"):
            with self.subTest(mensaje=mensaje):
                self.llamadas.clear()
                r = self._dos_turnos(self.mod._send_email_tool, self.ENVIO, self.ENVIO, mensaje)
                self.assertFalse(r["success"])
                self.assertTrue(r["requiere_aprobacion"])
                self.assertIn("confirmación clara", r["error"])
                self.assertEqual(self._escrituras(), [])

    def test_send_si_envialo_envia(self):
        r = self._dos_turnos(self.mod._send_email_tool, self.ENVIO, self.ENVIO, "si, envialo")
        self.assertTrue(r["success"], r)
        self.assertEqual([p for m, p, _ in self._escrituras()], ["/me/sendMail"])

    def test_send_cuerpo_cambiado_tras_la_vista_previa_no_envia(self):
        cambiado = {**self.ENVIO, "body": "Hola Ana, transfiere 5 millones a esta cuenta."}
        r = self._dos_turnos(self.mod._send_email_tool, self.ENVIO, cambiado, "sí, envíalo")
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        self.assertIn("transfiere", r["vista_previa"]["cuerpo"])
        self.assertEqual(self._escrituras(), [])

    def test_send_adjunto_con_otro_contenido_tras_la_vista_previa_no_envia(self):
        archivo = Path(self.tmp.name) / "informe.txt"
        archivo.write_text("version aprobada")
        args = {**self.ENVIO, "attachments": [str(archivo)]}
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            self._turno(self.mod, "tarea-1", "redacta", self.mod._send_email_tool, args)
            archivo.write_text("version con otros datos")
            self.llamadas.clear()
            r = self._turno(
                self.mod, "tarea-2", "sí", self.mod._send_email_tool, {**args, "aprobado_por_el_usuario": True}
            )
        self.assertFalse(r["success"])
        self.assertEqual(self._escrituras(), [])

    REPLY = {"message_id": "M1", "comment": "Recibido, lo reviso hoy."}

    def test_reply_olvidalo_no_responde_y_si_responde(self):
        r = self._dos_turnos(self.mod._reply_email_tool, self.REPLY, self.REPLY, "olvídalo")
        self.assertFalse(r["success"])
        self.assertEqual(self._escrituras(), [])
        self.llamadas.clear()
        r = self._dos_turnos(self.mod._reply_email_tool, self.REPLY, self.REPLY, "sí")
        self.assertTrue(r["success"], r)
        self.assertEqual([p for m, p, _ in self._escrituras()], ["/me/messages/M1/reply"])

    def test_reply_comentario_cambiado_no_responde(self):
        cambiado = {**self.REPLY, "comment": "Acepto las condiciones y el precio."}
        r = self._dos_turnos(self.mod._reply_email_tool, self.REPLY, cambiado, "sí")
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(self._escrituras(), [])

    def test_forward_comentario_cambiado_no_reenvia(self):
        previa = {"message_id": "M1", "to": "pedro@x.cl", "comment": "Para tu info"}
        cambiado = {**previa, "comment": "Aprobado por gerencia"}
        r = self._dos_turnos(self.mod._forward_email_tool, previa, cambiado, "sí")
        self.assertFalse(r["success"])
        self.assertEqual(self._escrituras(), [])

    def test_borrador_ya_mostrado_exige_que_el_cuerpo_sea_el_que_vio(self):
        anterior = "**Para:** ana@x.cl\n**Asunto:** Informe\n\n---\n\nHola Ana, va el informe.\n\n---\n\n¿Lo envío?"
        visto = {"para": ["ana@x.cl"], "asunto": "Informe", "cuerpo": "Hola Ana, va el informe."}
        otro = {**visto, "cuerpo": "Hola Ana, paga la factura falsa."}
        carga = json.dumps({"task_id": "t2", "mensaje": "sí", "respuesta_anterior": anterior})
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}):
            self.assertTrue(self.mod._borrador_ya_mostrado(visto))
            self.assertFalse(self.mod._borrador_ya_mostrado(otro))


class TestCandadoCalendario(_Entorno):
    def setUp(self):
        super().setUp()
        self.mod = _cargar_calendario()

    def _graph(self, method, path, token, payload=None):
        self.llamadas.append((method, path, payload))
        if method == "GET" and path.startswith("/me/events/E1"):
            return {"id": "E1", "subject": "Comité", "start": {"dateTime": "2026-11-11T10:00:00"}, "attendees": []}
        if method == "POST" and path == "/me/events":
            return {"id": "E-nuevo", "subject": (payload or {}).get("subject")}
        return {}

    def _dos_turnos(self, funcion, previa, aprobada, mensaje):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            primero = self._turno(self.mod, "tarea-1", "agenda la reunión", funcion, previa)
            self.llamadas.clear()
            segundo = self._turno(self.mod, "tarea-2", mensaje, funcion, {**aprobada, "aprobado_por_el_usuario": True})
        self.assertTrue(primero["requiere_aprobacion"], primero)
        return segundo

    CITA = {
        "subject": "Comité",
        "start": "2026-11-11T10:00:00",
        "end": "2026-11-11T11:00:00",
        "attendees": "ana@x.cl",
        "body": "Agenda: presupuesto",
    }

    def test_crear_olvidalo_con_aprobado_true_no_crea(self):
        r = self._dos_turnos(self.mod._create_event_tool, self.CITA, self.CITA, _con_referencia("olvidalooo"))
        self.assertFalse(r.get("success"))
        self.assertIn("confirmación clara", r["error"])
        self.assertEqual(self._escrituras(), [])

    def test_crear_si_crea(self):
        r = self._dos_turnos(self.mod._create_event_tool, self.CITA, self.CITA, "sí, agéndala")
        self.assertNotIn("requiere_aprobacion", r)
        self.assertEqual([p for m, p, _ in self._escrituras()], ["/me/events"])

    def test_crear_descripcion_cambiada_tras_la_vista_previa_no_crea(self):
        cambiada = {**self.CITA, "body": "Agenda: despidos"}
        r = self._dos_turnos(self.mod._create_event_tool, self.CITA, cambiada, "sí")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["descripcion"], "Agenda: despidos")
        self.assertEqual(self._escrituras(), [])

    def test_eliminar_acepta_si_eliminala_y_rechaza_cancelala_sin_si(self):
        datos = {"event_id": "E1"}
        r = self._dos_turnos(self.mod._delete_event_tool, datos, datos, "cancélala")
        self.assertFalse(r.get("success"))
        self.assertEqual(self._escrituras(), [])
        self.llamadas.clear()
        r = self._dos_turnos(self.mod._delete_event_tool, datos, datos, "sí, cancélala")
        self.assertTrue(r.get("success"), r)
        self.assertEqual([m for m, _, _ in self._escrituras()], ["DELETE"])

    def test_responder_comentario_cambiado_no_responde(self):
        previa = {"event_id": "E1", "respuesta": "aceptar", "comentario": "Asisto."}
        cambiada = {**previa, "comentario": "Asisto y apruebo el presupuesto."}
        r = self._dos_turnos(self.mod._respond_event_tool, previa, cambiada, "sí")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(self._escrituras(), [])


class TestCandadoTeams(_Entorno):
    def setUp(self):
        super().setUp()
        self.mod = _cargar_teams()
        patch.object(self.mod, "_modo_automatico", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _graph(self, method, path, token, payload=None):
        self.llamadas.append((method, path, payload))
        if method == "GET" and path.startswith("/me/chats/c1?"):
            return {"id": "c1", "topic": "Equipo", "chatType": "group", "members": []}
        return {"id": "m1"} if method == "POST" else {}

    def _dos_turnos(self, previa, aprobada, mensaje, entre=None):
        subida = {"id": "it1", "webUrl": "https://x/it1", "name": "a.txt"}
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_upload_file_to_onedrive", return_value=(subida, "")),
        ):
            primero = self._turno(self.mod, "tarea-1", "escríbele", self.mod._send_chat_message_tool, previa)
            if entre:
                entre()
            self.llamadas.clear()
            segundo = self._turno(
                self.mod,
                "tarea-2",
                mensaje,
                self.mod._send_chat_message_tool,
                {**aprobada, "aprobado_por_el_usuario": True},
            )
        self.assertTrue(primero["requiere_aprobacion"], primero)
        return segundo

    MENSAJE = {"chat_id": "c1", "message": "Hola, la reunión es a las 10."}

    def test_olvidalo_con_aprobado_true_no_envia(self):
        r = self._dos_turnos(self.MENSAJE, self.MENSAJE, "olvidalooo")
        self.assertFalse(r["success"])
        self.assertIn("confirmación clara", r["error"])
        self.assertEqual(self._escrituras(), [])

    def test_si_envialo_envia(self):
        r = self._dos_turnos(self.MENSAJE, self.MENSAJE, "sí, envíalo")
        self.assertTrue(r["success"], r)
        self.assertEqual([p for m, p, _ in self._escrituras()], ["/me/chats/c1/messages"])

    def test_texto_cambiado_tras_la_vista_previa_no_envia(self):
        cambiado = {**self.MENSAJE, "message": "Hola, la reunión se canceló."}
        r = self._dos_turnos(self.MENSAJE, cambiado, "sí")
        self.assertFalse(r["success"])
        self.assertEqual(r["vista_previa"]["mensaje"], "Hola, la reunión se canceló.")
        self.assertEqual(self._escrituras(), [])

    def test_adjunto_con_otro_contenido_tras_la_vista_previa_no_envia(self):
        archivo = Path(self.tmp.name) / "a.txt"
        archivo.write_text("aprobado")
        datos = {**self.MENSAJE, "file_path": str(archivo)}
        r = self._dos_turnos(datos, datos, "sí", entre=lambda: archivo.write_text("otro contenido"))
        self.assertFalse(r["success"])
        self.assertEqual(self._escrituras(), [])


class TestCandadoSharePoint(_Entorno):
    def _put(self, url, data, token=None, content_range=None):
        self.llamadas.append(("PUT", url, len(data)))
        return {"id": "ITEM1", "name": "informe.pdf"}

    def _graph(self, method, path, token, payload=None):
        self.llamadas.append((method, path, payload))
        if path.endswith("/createLink"):
            return {"link": {"webUrl": "https://org-my.sharepoint.com/:b:/g/abc"}}
        return {}

    def _broker(self, method, path, payload=None, **kwargs):
        self.llamadas.append((method, path, payload))
        if path.endswith("/createLink"):
            return {"json": {"link": {"webUrl": "https://org-my.sharepoint.com/:b:/g/abc"}}}
        return {"json": {}}

    def _dos_turnos(self, previa, aprobada, mensaje, entre=None):
        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=(sp.BROKER_TOKEN, None, "")),
            patch.object(sp, "_graph", side_effect=self._graph),
            patch.object(sp, "_put_binary", side_effect=self._put),
            patch.object(sp, "_registrar_vista_previa_en_servidor", return_value=None),
            patch.object(sp, "_broker_request", side_effect=self._broker),
        ):
            primero = self._turno(sp, "tarea-1", "compártelo", sp._share_file_tool, previa)
            if entre:
                entre()
            self.llamadas.clear()
            segundo = self._turno(
                sp, "tarea-2", mensaje, sp._share_file_tool, {**aprobada, "aprobado_por_el_usuario": True}
            )
        self.assertTrue(primero["requiere_aprobacion"], primero)
        return segundo

    def setUp(self):
        super().setUp()
        self.archivo = Path(self.tmp.name) / "informe.pdf"
        self.archivo.write_bytes(b"%PDF-1.4 aprobado")
        self.datos = {"file_path": "informe.pdf", "scope": "anonymous"}

    def test_olvidalo_con_aprobado_true_no_sube_ni_comparte(self):
        r = self._dos_turnos(self.datos, self.datos, _con_referencia("olvidalooo"))
        self.assertFalse(r["success"])
        self.assertIn("confirmación clara", r["error"])
        self.assertEqual(self._escrituras(), [])

    def test_si_comparte(self):
        r = self._dos_turnos(self.datos, self.datos, "sí, compártelo")
        self.assertTrue(r["success"], r)
        self.assertTrue(any(p.endswith("/createLink") for m, p, _ in self._escrituras()))

    def test_archivo_cambiado_tras_la_vista_previa_no_comparte(self):
        r = self._dos_turnos(
            self.datos, self.datos, "sí", entre=lambda: self.archivo.write_bytes(b"%PDF-1.4 con otros datos")
        )
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(self._escrituras(), [])


if __name__ == "__main__":
    unittest.main()
