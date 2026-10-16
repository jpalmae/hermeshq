import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RUTA = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)


def _cargar():
    spec = importlib.util.spec_from_file_location("teams_plugin_under_test", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


PERSONA = {"id": "u-jon", "displayName": "Jorge Soto", "mail": "jsoto@external.example.com"}


class TestSendToPerson(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.llamadas = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _ejecutar(
        self, args, usuarios=None, chats=None, crear_error=None, envio_error=None, vista_previa=True, tarea="tarea-2"
    ):
        def graph(method, path, token, payload=None):
            self.llamadas.append((method, path, payload))
            if path.startswith("/users?"):
                return {"value": [PERSONA] if usuarios is None else usuarios}
            if method == "POST" and path.endswith("/messages"):
                return {"error": envio_error} if envio_error else {"id": "m1"}
            if path.startswith("/me/chats"):
                return {"value": chats or []}
            if path.startswith("/me?"):
                return {"id": "u-yo"}
            if method == "POST" and path == "/chats":
                return {"error": crear_error} if crear_error else {"id": "chat-nuevo"}
            raise AssertionError(path)

        def correr(argumentos, tarea_actual):
            with patch.object(self.mod, "_current_task_id", return_value=tarea_actual):
                return json.loads(self.mod._send_to_person_tool(argumentos))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            if args.get("aprobado_por_el_usuario") and vista_previa:
                correr({k: v for k, v in args.items() if k != "aprobado_por_el_usuario"}, "tarea-1")
            return correr(args, tarea)

    def _enviados(self):
        return [c for c in self.llamadas if c[0] == "POST" and c[1].endswith("/messages")]

    def test_without_approval_it_only_returns_a_preview_and_sends_nothing(self):
        r = self._ejecutar({"email": "jsoto@external.example.com", "message": "Hola"})
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["para"]["nombre"], "Jorge Soto")
        self.assertEqual(r["vista_previa"]["mensaje"], "Hola")
        self.assertEqual(self._enviados(), [])
        self.assertFalse(any(c[0] == "POST" for c in self.llamadas))

    def test_with_approval_it_reuses_the_existing_direct_chat(self):
        chats = [
            {"id": "otro", "members": [{"userId": "u-x"}, {"userId": "u-yo"}]},
            {"id": "chat-existente", "members": [{"userId": "u-yo"}, {"userId": "u-jon"}]},
        ]
        r = self._ejecutar(
            {"email": "jsoto@external.example.com", "message": "Hola", "aprobado_por_el_usuario": True}, chats=chats
        )
        self.assertTrue(r["success"], r)
        self.assertEqual((r["chat_id"], r["chat_creado"]), ("chat-existente", False))
        self.assertFalse(any(c[1] == "/chats" for c in self.llamadas))
        self.assertEqual(self._enviados()[0][1], "/me/chats/chat-existente/messages")
        self.assertEqual(self._enviados()[0][2], {"body": {"contentType": "text", "content": "Hola"}})

    def test_without_a_previous_chat_it_creates_one_with_both_members(self):
        r = self._ejecutar({"email": "jsoto@external.example.com", "message": "Hola", "aprobado_por_el_usuario": True})
        self.assertTrue(r["success"], r)
        self.assertEqual((r["chat_id"], r["chat_creado"]), ("chat-nuevo", True))
        crear = next(c for c in self.llamadas if c[1] == "/chats")
        self.assertEqual(crear[2]["chatType"], "oneOnOne")
        enlaces = [m["user@odata.bind"] for m in crear[2]["members"]]
        self.assertTrue(enlaces[0].endswith("users('u-yo')") and enlaces[1].endswith("users('u-jon')"))
        self.assertEqual(self._enviados()[0][1], "/me/chats/chat-nuevo/messages")

    def test_missing_chat_create_permission_is_explained(self):
        r = self._ejecutar(
            {"email": "jsoto@external.example.com", "message": "Hola", "aprobado_por_el_usuario": True},
            crear_error="Sin permiso para acceder a este recurso (403).",
        )
        self.assertFalse(r["success"])
        self.assertIn("Chat.Create", r["error"])
        self.assertEqual(self._enviados(), [])

    def test_person_not_in_the_directory_and_bad_input_are_reported(self):
        r = self._ejecutar({"email": "nadie@x.cl", "message": "Hola", "aprobado_por_el_usuario": True}, usuarios=[])
        self.assertFalse(r["success"])
        self.assertIn("No encontré", r["error"])
        r = self._ejecutar({"email": "Jonathan", "message": "Hola"})
        self.assertIn("ms365_find_person", r["error"])
        r = self._ejecutar({"email": "a@x.cl"})
        self.assertFalse(r["success"])

    def test_a_failure_while_sending_is_reported_with_the_chat(self):
        r = self._ejecutar(
            {"email": "jsoto@external.example.com", "message": "Hola", "aprobado_por_el_usuario": True},
            chats=[{"id": "c1", "members": [{"userId": "u-jon"}]}],
            envio_error="Error 500",
        )
        self.assertFalse(r["success"])
        self.assertEqual(r["chat_id"], "c1")

    def test_single_quotes_in_the_address_are_escaped_in_the_filter(self):
        self._ejecutar({"email": "o'brien@x.cl", "message": "Hola"})
        ruta = next(c[1] for c in self.llamadas if c[1].startswith("/users?"))
        self.assertIn("o%27%27brien", ruta)

    def _datos(self):
        return {"email": PERSONA["mail"], "message": "Aviso de prueba", "aprobado_por_el_usuario": True}

    def test_an_approval_without_a_previous_preview_is_refused_and_sends_nothing(self):
        r = self._ejecutar(self._datos(), vista_previa=False)
        self.assertFalse(r["success"])
        self.assertTrue(r["requiere_aprobacion"])
        self.assertIn("No se envió", r["error"])
        self.assertEqual(self._enviados(), [])

    def test_the_agent_cannot_approve_in_the_same_task_that_showed_the_preview(self):
        r = self._ejecutar(self._datos(), tarea="tarea-1")
        self.assertFalse(r["success"])
        self.assertEqual(self._enviados(), [])

    def test_changing_the_text_after_the_preview_needs_a_new_preview(self):
        self._ejecutar({**self._datos(), "message": "Texto distinto"}, vista_previa=False)
        r = self._ejecutar(self._datos(), vista_previa=False)
        self.assertFalse(r["success"])
        self.assertEqual(self._enviados(), [])

    def test_an_expired_preview_does_not_count(self):
        with patch.object(self.mod.time, "time", return_value=1_000_000.0):
            self._ejecutar({k: v for k, v in self._datos().items() if k != "aprobado_por_el_usuario"})
        with patch.object(self.mod.time, "time", return_value=1_000_000.0 + 3600):
            r = self._ejecutar(self._datos(), vista_previa=False)
        self.assertFalse(r["success"])
        self.assertEqual(self._enviados(), [])

    def test_a_preview_is_used_once(self):
        primero = self._ejecutar(self._datos(), chats=[{"id": "c1", "members": [{"userId": "u-jon"}]}])
        self.assertTrue(primero["success"], primero)
        segundo = self._ejecutar(
            self._datos(), vista_previa=False, chats=[{"id": "c1", "members": [{"userId": "u-jon"}]}]
        )
        self.assertFalse(segundo["success"])
        self.assertEqual(len(self._enviados()), 1)

    def test_the_tool_is_registered_and_listed_in_the_manifest(self):
        class Ctx:
            def __init__(self):
                self.nombres = []

            def register_tool(self, **kwargs):
                self.nombres.append(kwargs["name"])

        ctx = Ctx()
        self.mod.register(ctx)
        self.assertIn("teams_send_to_person", ctx.nombres)
        manifiesto = RUTA.with_name("plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("teams_send_to_person", manifiesto)


if __name__ == "__main__":
    unittest.main()
