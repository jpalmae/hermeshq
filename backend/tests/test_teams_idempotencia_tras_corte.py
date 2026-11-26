import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

RUTA = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)
CHAT = "19:grupo@thread.v2"
TEXTO = "Reunión de coordinación mañana a las 10"
YO = {"id": "yo", "mail": "yo@hq.example", "userPrincipalName": "yo@hq.example", "displayName": "Yo"}


def _cargar():
    spec = importlib.util.spec_from_file_location("teams_idempotencia", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.posts = []
        self.chat = []
        self.fallo_post = None
        self.fallo_get = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _graph(self, method, path, token, payload=None):
        if method == "POST" and path.endswith("/messages"):
            self.posts.append(payload)
            if self.fallo_post == "timeout_aceptado":
                self.chat.append(
                    {
                        "messageType": "message",
                        "createdDateTime": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "from": {"user": {"id": "yo"}},
                        "body": {"contentType": "text", "content": payload["body"]["content"]},
                    }
                )
                raise TimeoutError("timed out")
            if self.fallo_post == "timeout_perdido":
                raise TimeoutError("timed out")
            if self.fallo_post == "http_500":
                return {"error": "Error 500: boom", "_graph_code": "x"}
            return {"id": "m1"}
        if path.startswith("/me?"):
            return YO
        if "/messages" in path and method == "GET":
            if self.fallo_get:
                return {"error": "Error 503: no"}
            return {"value": list(reversed(self.chat))}
        if path.startswith("/me/chats?"):
            return {"value": [{"id": CHAT, "topic": "Grupo", "chatType": "group", "members": [{"displayName": "A"}]}]}
        for ruta in (f"/me/chats/{CHAT}?",):
            if path.startswith(ruta):
                return {"id": CHAT, "topic": "Grupo", "chatType": "group", "members": [{"displayName": "A"}]}
        return {"error": "Chat o elemento no encontrado (404)."}

    def _turno(self, tarea, mensaje, **args):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_modo_automatico", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})}),
        ):
            try:
                return json.loads(self.mod._send_chat_message_tool(args))
            except TimeoutError:
                return {"excepcion": "timeout"}

    def _aprobar(self):
        self._turno("t1", "envía", chat_nombre="Grupo", message=TEXTO)


class TestEnvioTrasCorte(_Base):
    def test_timeout_con_graph_que_acepto_el_reintento_no_duplica(self):
        self._aprobar()
        self.fallo_post = "timeout_aceptado"
        self.assertEqual(
            self._turno("t2", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True),
            {"excepcion": "timeout"},
        )
        self.fallo_post = None
        r = self._turno("t3", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and r["ya_enviado"], r)
        self.assertEqual(len(self.posts), 1)

    def test_control_negativo_timeout_sin_que_graph_acepte_el_reintento_envia_una_vez(self):
        self._aprobar()
        self.fallo_post = "timeout_perdido"
        self._turno("t2", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.fallo_post = None
        r = self._turno("t3", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and not r.get("ya_enviado"), r)
        self.assertEqual(len(self.posts), 2)

    def test_si_no_se_puede_revisar_el_chat_no_reenvia(self):
        self._aprobar()
        self.fallo_post = "timeout_aceptado"
        self._turno("t2", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.fallo_post = None
        self.fallo_get = True
        r = self._turno("t3", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertFalse(r["success"])
        self.assertIn("No lo reenvío", r["error"])
        self.assertEqual(len(self.posts), 1)

    def test_rechazo_firme_de_graph_permite_reintentar_sin_revisar(self):
        self._aprobar()
        self.fallo_post = "http_500"
        r = self._turno("t2", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertFalse(r["success"])
        self.fallo_post = None
        r = self._turno("t3", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and not r.get("ya_enviado"), r)
        self.assertEqual(len(self.posts), 2)

    def test_un_envio_normal_no_consulta_el_chat(self):
        self._aprobar()
        r = self._turno("t2", "sí", chat_nombre="Grupo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertTrue(r["success"])
        self.assertEqual(len(self.posts), 1)
