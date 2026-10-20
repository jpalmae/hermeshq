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
            return {}

        return graph

    def _ejecutar(self, funcion, args, tarea="tarea-2", previa=True, error_en=None):
        def correr(argumentos, actual):
            with patch.object(self.mod, "_current_task_id", return_value=actual):
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
        return {"message_id": "M1", "to": ["gonzalo@example.com"], "comment": "Para tu revisión", **extra}

    def test_the_preview_lists_what_travels_with_the_original_and_sends_nothing(self):
        r = self._ejecutar(self.mod._forward_email_tool, self._datos())
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        vista = r["vista_previa"]
        self.assertEqual(vista["asunto"], "Ticket 4521: acceso USB")
        self.assertEqual(vista["para"], ["gonzalo@example.com"])
        nombres = [a["nombre"] for a in vista["adjuntos_e_imagenes_que_viajan"]]
        self.assertEqual(nombres, ["logo.png", "captura-ticket.png", "detalle.pdf"])
        self.assertEqual(self._acciones(), [])

    def test_with_a_real_approval_it_forwards_the_original_message(self):
        r = self._ejecutar(self.mod._forward_email_tool, self._datos(aprobado_por_el_usuario=True))
        self.assertTrue(r["success"], r)
        accion = self._acciones()[0]
        self.assertEqual(accion[1], "/me/messages/M1/forward")
        self.assertEqual(accion[2]["toRecipients"][0]["emailAddress"]["address"], "gonzalo@example.com")
        self.assertEqual(accion[2]["comment"], "Para tu revisión")

    def test_the_agent_cannot_approve_in_the_same_task_or_without_a_preview(self):
        mismo = self._ejecutar(self.mod._forward_email_tool, self._datos(aprobado_por_el_usuario=True), tarea="tarea-1")
        self.assertFalse(mismo["success"])
        sin = self._ejecutar(
            self.mod._forward_email_tool, self._datos(comment="otro", aprobado_por_el_usuario=True), previa=False
        )
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
            self._datos(cc=["jefe@example.com"], aprobado_por_el_usuario=True),
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
        r = self._ejecutar(self.mod._forward_email_tool, {"message_id": "M1", "to": ["Gonzalo"]})
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
