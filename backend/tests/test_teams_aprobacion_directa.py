from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)
PERSONA = {"id": "p-1", "displayName": "Víctor Ortiz (SUC1)", "mail": "vortiz@contoso.cl"}
MENSAJE = "Prueba auditoría TEAMS-2 (mensaje automático de prueba, no requiere respuesta)"
ARGS_PERSONA = {"email": "vortiz@contoso.cl", "message": MENSAJE}
ARGS_CHAT = {"chat_nombre": "Equipo Pruebas", "message": MENSAJE}


def _load():
    spec = importlib.util.spec_from_file_location("teams_directo", PLUGIN_PATH)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.envios = []

    def _post(self, token, chat_id, message, file_path):
        self.envios.append((chat_id, message))
        return json.dumps({"success": True, "message": "Mensaje enviado correctamente.", "id": "m-1"})

    def _con(self, tarea, mensaje, anterior="", conv="s1", metadata=None):
        payload = {
            "task_id": tarea,
            "mensaje": mensaje,
            "respuesta_anterior": anterior,
            "metadata": {"public_session_id": conv, **(metadata or {})},
        }
        return (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_token_herramienta", return_value=("tok", "")),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_find_user", return_value=(PERSONA, "")),
            patch.object(self.mod, "_find_direct_chat", return_value="chat-directo"),
            patch.object(self.mod, "_resolver_chat", return_value=("chat-1", "Equipo Pruebas", None)),
            patch.object(self.mod, "_post_chat_message", side_effect=self._post),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
        )

    def _en(self, ctx, funcion, *args):
        with (
            ctx[0],
            ctx[1],
            ctx[2],
            ctx[3],
            ctx[4],
            ctx[5],
            ctx[6],
            ctx[7],
        ):
            return funcion(*args)

    def _vista(self, que="persona", tarea="t1", conv="s1"):
        if que == "persona":
            funcion, args, nombre = self.mod._send_to_person_tool, ARGS_PERSONA, "teams_send_to_person"
        else:
            funcion, args, nombre = self.mod._send_chat_message_tool, ARGS_CHAT, "teams_send_chat_message"
        out = json.loads(self._en(self._con(tarea, "envíalo", conv=conv), funcion, dict(args)))
        assert out.get("requiere_aprobacion"), out
        self.vista_mostrada = runner.texto_vista_teams(nombre, out["vista_previa"])
        runner._marcar_vista_mostrada({"task_id": tarea}, self.vista_mostrada)
        return out

    def _confirmar(self, mensaje="sí", tarea="t2", anterior=None, conv="s1", metadata=None):
        anterior = self.vista_mostrada if anterior is None else anterior
        return self._en(self._con(tarea, mensaje, anterior, conv, metadata), self.mod.ejecutar_aprobacion_pendiente)


class TestEnviaLoAprobado(_Base):
    def test_a_una_persona_con_un_solo_si_y_sin_duplicar(self):
        self._vista("persona")
        self.assertEqual(self.envios, [])
        hecho = self._confirmar()
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual(hecho["herramienta"], "teams_send_to_person")
        self.assertEqual(self.envios, [("chat-directo", MENSAJE)])
        self.assertIsNone(self._confirmar(tarea="t3"))
        self.assertEqual(len(self.envios), 1)

    def test_a_un_chat_con_un_solo_si(self):
        self._vista("chat")
        hecho = self._confirmar("sí, envíalo")
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual(hecho["herramienta"], "teams_send_chat_message")
        self.assertEqual(self.envios, [("chat-1", MENSAJE)])

    def test_caso_real_el_si_envia_aunque_el_modelo_no_haga_nada(self):
        self._vista("persona")
        hecho = self._confirmar("Sí")
        self.assertEqual(hecho["args"]["email"], "vortiz@contoso.cl")
        self.assertTrue(hecho["args"]["aprobado_por_el_usuario"])
        self.assertEqual(hecho["preview"]["para"]["correo"], "vortiz@contoso.cl")


class TestNoEnviaSinConfirmacionValida(_Base):
    def _nada(self, **kw):
        self.assertIsNone(self._confirmar(**kw))
        self.assertEqual(self.envios, [])

    def setUp(self):
        super().setUp()
        self._vista("persona")

    def test_respuestas_que_no_son_un_si_puro(self):
        for mensaje in (
            "no",
            "mejor no",
            "espera",
            "sí, pero cámbialo a otro texto",
            "¿sí?",
            "Sí, envía ahora el mensaje a Víctor Ortiz (SUC1) tal como lo mostraste en la vista previa anterior",
            "Qué hora es",
        ):
            with self.subTest(mensaje):
                self._nada(mensaje=mensaje)

    def test_si_la_vista_mostrada_no_es_la_del_servidor(self):
        self._nada(anterior="Vista previa del mensaje:\n- Para: Tomás\n- Mensaje: otro texto\n\n¿Lo envío?")

    def test_sin_mensaje_anterior(self):
        self._nada(anterior="")

    def test_otra_conversacion(self):
        self._nada(conv="otra")

    def test_la_misma_tarea_que_registro_la_vista(self):
        self._nada(tarea="t1")

    def test_modo_automatico(self):
        self._nada(metadata={"scheduled": True})

    def test_pendiente_vencido(self):
        ruta = Path(self.tmp.name) / "work" / ".teams_pendiente.json"
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        datos["ts"] = time.time() - 5 * 3600
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        self._nada()

    def test_sin_pendiente(self):
        (Path(self.tmp.name) / "work" / ".teams_pendiente.json").unlink()
        self._nada()

    def test_una_segunda_vista_reemplaza_a_la_primera(self):
        otra = {**ARGS_PERSONA, "message": "Otro texto distinto"}
        out = json.loads(self._en(self._con("t5", "envíalo"), self.mod._send_to_person_tool, dict(otra)))
        self.vista_mostrada = runner.texto_vista_teams("teams_send_to_person", out["vista_previa"])
        runner._marcar_vista_mostrada({"task_id": "t5"}, self.vista_mostrada)
        hecho = self._confirmar(tarea="t6")
        self.assertEqual(self.envios, [("chat-directo", "Otro texto distinto")])
        self.assertTrue(hecho["resultado"]["success"])


class TestRunner(unittest.TestCase):
    VISTA = {
        "para": {"nombre": "Víctor Ortiz (SUC1)", "correo": "vortiz@contoso.cl"},
        "mensaje": MENSAJE,
    }

    def _ejecucion(self, resultado):
        return {
            "herramienta": "teams_send_to_person",
            "args": {**ARGS_PERSONA, "aprobado_por_el_usuario": True},
            "resultado": resultado,
            "preview": self.VISTA,
        }

    def _directo(self, ejecucion):
        plugin = SimpleNamespace(ejecutar_aprobacion_pendiente=lambda: ejecucion)
        payload = {"task_id": "t2", "hermes_home": "/x", "prompt": "sí", "metadata": {"source": "public_chat"}}
        with (
            patch.object(runner, "_cargar_plugin_teams", return_value=plugin),
            patch.object(runner, "_chat_con_usuario", return_value=True),
        ):
            return runner._teams_aprobado_directo(payload)

    def test_texto_de_la_vista_previa_sale_de_lo_registrado(self):
        texto = runner.texto_vista_teams("teams_send_to_person", self.VISTA)
        self.assertIn("**Para:** Víctor Ortiz (SUC1) — vortiz@contoso.cl", texto)
        self.assertIn(f"**Mensaje:** {MENSAJE}", texto)
        self.assertTrue(texto.endswith("¿Lo envío? Responde «sí» para confirmar."))

    def test_vista_de_chat_y_archivo(self):
        texto = runner.texto_vista_teams(
            "teams_send_chat_message", {"chat": "Equipo X", "mensaje": "hola", "archivo": "outputs/a.docx"}
        )
        self.assertIn("**Chat:** Equipo X", texto)
        self.assertIn("**Archivo:** a.docx", texto)

    def test_vista_del_servidor_en_un_turno_con_la_herramienta(self):
        mensajes = [
            {"role": "user", "content": "envíale a Tomás"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "teams_send_to_person"}}]},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": json.dumps({"success": False, "requiere_aprobacion": True, "vista_previa": self.VISTA}),
            },
            {"role": "assistant", "content": "texto del modelo"},
        ]
        self.assertEqual(
            runner.vista_teams_del_servidor(mensajes, 0), runner.texto_vista_teams("teams_send_to_person", self.VISTA)
        )

    def test_sin_herramienta_no_hay_vista_del_servidor(self):
        self.assertIsNone(runner.vista_teams_del_servidor([{"role": "user", "content": "hola"}], 0))

    def test_despues_de_un_envio_exitoso_ya_no_hay_vista(self):
        mensajes = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "teams_send_to_person"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True})},
        ]
        self.assertIsNone(runner.vista_teams_del_servidor(mensajes, 0))

    def test_exito_confirmado_por_microsoft_365(self):
        res = self._directo(self._ejecucion({"success": True, "id": "m-1"}))
        self.assertTrue(res["final_response"].startswith("✅ Mensaje enviado por Teams"))
        self.assertIn("Víctor Ortiz (SUC1)", res["final_response"])
        self.assertEqual([c["name"] for c in res["tool_calls"]], ["teams_send_to_person"])
        self.assertEqual(res["iterations"], 0)

    def test_fallo_se_informa_sin_afirmar_nada(self):
        res = self._directo(self._ejecucion({"success": False, "error": "Error 403"}))
        self.assertTrue(res["final_response"].startswith("❌ No se pudo enviar el mensaje por Teams"))
        self.assertIn("No se envió nada", res["final_response"])
        self.assertNotIn("✅", res["final_response"])

    def test_sin_aprobacion_valida_sigue_el_modelo(self):
        self.assertIsNone(self._directo(None))
        self.assertIsNone(self._directo(self._ejecucion({"success": False, "requiere_aprobacion": True})))

    def test_marcar_vista_mostrada_solo_en_la_tarea_que_la_registro(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HERMES_SESSION_CWD": tmp}):
            ruta = Path(tmp) / "work" / ".teams_pendiente.json"
            ruta.parent.mkdir()
            ruta.write_text(json.dumps({"task": "t1"}), encoding="utf-8")
            runner._marcar_vista_mostrada({"task_id": "otra"}, "x")
            self.assertNotIn("mostrada", json.loads(ruta.read_text(encoding="utf-8")))
            runner._marcar_vista_mostrada({"task_id": "t1"}, "x")
            self.assertEqual(json.loads(ruta.read_text(encoding="utf-8"))["mostrada"], "x")


if __name__ == "__main__":
    unittest.main()
