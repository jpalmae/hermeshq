from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)
CORREO = "vortiz@contoso.cl"
MENSAJE = "Prueba auditoría TEAMS-2 (mensaje automático de prueba, no requiere respuesta)"


class _Respuesta:
    def __init__(self, datos):
        self._cuerpo = json.dumps(datos).encode()

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _GraphSimulado:
    def __init__(self):
        self.escrituras = []

    def __call__(self, req, timeout=None):
        url, metodo = req.full_url, req.get_method()
        if "/control/m365/agent-token" in url:
            return _Respuesta({"access_token": "tok-simulado"})
        if metodo == "GET" and "/users?" in url:
            return _Respuesta({"value": [{"id": "p-1", "displayName": "Víctor Ortiz (SUC1)", "mail": CORREO}]})
        if metodo == "GET" and "/me/chats" in url:
            return _Respuesta({"value": [{"id": "chat-directo", "members": [{"userId": "p-1"}]}]})
        if metodo == "POST" and url.endswith("/me/chats/chat-directo/messages"):
            self.escrituras.append(json.loads(req.data.decode()))
            return _Respuesta({"id": "msg-1"})
        raise AssertionError(f"llamada inesperada a Graph: {metodo} {url}")


class TestE2ERunnerConPluginReal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "hermes"
        (self.home / "plugins" / "hermeshq_ms365_teams").mkdir(parents=True)
        shutil.copy(PLUGIN, self.home / "plugins" / "hermeshq_ms365_teams" / "__init__.py")
        self.cwd = Path(self.tmp.name) / "ws"
        self.cwd.mkdir()
        self.graph = _GraphSimulado()
        for parche in (
            patch.object(urllib.request, "urlopen", self.graph),
            patch.dict(
                os.environ,
                {
                    "HERMES_SESSION_CWD": str(self.cwd),
                    "HERMESHQ_INTERNAL_API_URL": "http://hq.local/api",
                    "HERMESHQ_AGENT_ID": "a1",
                    "HERMESHQ_AGENT_TOKEN": "t",
                },
            ),
            patch.object(runner, "_diag", lambda *a, **k: None),
        ):
            parche.start()
            self.addCleanup(parche.stop)

    def _payload(self, tarea, mensaje, anterior="", conv="s1"):
        return {
            "task_id": tarea,
            "prompt": mensaje,
            "mensaje": mensaje,
            "respuesta_anterior": anterior,
            "hermes_home": str(self.home),
            "metadata": {"source": "public_chat", "public_session_id": conv, "thread_user_id": "u1"},
        }

    def _turno(self, payload, accion):
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}):
            return accion(payload)

    def _vista_del_servidor(self, tarea="t1"):
        payload = self._payload(tarea, f"Envíale a Víctor Ortiz por Teams: {MENSAJE}")

        def modelo(p):
            plugin = runner._cargar_plugin_teams(p["hermes_home"])
            resultado = plugin._send_to_person_tool({"email": CORREO, "message": MENSAJE})
            mensajes = [
                {"role": "user", "content": p["prompt"]},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "function": {"name": "teams_send_to_person", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": resultado},
            ]
            texto = runner.vista_teams_del_servidor(mensajes, 0)
            runner._marcar_vista_mostrada(p, texto)
            return json.loads(resultado), texto

        resultado, texto = self._turno(payload, modelo)
        self.assertTrue(resultado["requiere_aprobacion"], resultado)
        self.assertEqual(self.graph.escrituras, [], "la vista previa no envía nada")
        return texto

    def _si(self, tarea, mensaje="sí", anterior="", conv="s1"):
        return self._turno(self._payload(tarea, mensaje, anterior, conv), runner._teams_aprobado_directo)

    def test_el_si_envia_una_sola_vez_y_confirma_con_lo_que_dijo_el_servidor(self):
        vista = self._vista_del_servidor()
        resultado = self._si("t2", "sí", anterior=vista)
        self.assertTrue(resultado["final_response"].startswith("✅ Mensaje enviado por Teams"), resultado)
        self.assertIn("Víctor Ortiz (SUC1)", resultado["final_response"])
        self.assertEqual(len(self.graph.escrituras), 1)
        self.assertEqual(self.graph.escrituras[0]["body"]["content"], MENSAJE)
        self.assertEqual([c["name"] for c in resultado["tool_calls"]], ["teams_send_to_person"])
        self.assertIsNone(self._si("t3", "sí", anterior=resultado["final_response"]))
        self.assertEqual(len(self.graph.escrituras), 1, "un segundo «sí» no reenvía")

    def test_control_negativo_vista_escrita_a_mano_por_el_modelo(self):
        self._vista_del_servidor()
        a_mano = f"Vista previa del mensaje:\n- Para: Tomás\n- Mensaje: {MENSAJE}\n\n¿Lo envío?"
        self.assertIsNone(self._si("t2", "sí", anterior=a_mano))
        self.assertEqual(self.graph.escrituras, [])

    def test_control_negativo_otra_conversacion_y_no_rotundo(self):
        vista = self._vista_del_servidor()
        self.assertIsNone(self._si("t2", "sí", anterior=vista, conv="otra"))
        self.assertIsNone(self._si("t2", "mejor no", anterior=vista))
        self.assertIsNone(self._si("t2", "no", anterior=vista))
        self.assertEqual(self.graph.escrituras, [])

    def test_control_negativo_sin_vista_previa_previa(self):
        self.assertIsNone(self._si("t2", "sí", anterior="Vista previa del mensaje... ¿Lo envío?"))
        self.assertEqual(self.graph.escrituras, [])


if __name__ == "__main__":
    unittest.main()
