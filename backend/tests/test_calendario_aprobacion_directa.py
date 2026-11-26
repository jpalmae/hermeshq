from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)

EVENTO = {
    "id": "ev-real-1",
    "subject": "Prueba HermesHQ lote 1",
    "isAllDay": False,
    "start": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-12T18:30:00.0000000", "timeZone": "UTC"},
    "attendees": [],
    "webLink": "https://outlook/ev-real-1",
}
VISTA_A_MANO = (
    "📅 **Vista previa del cambio** (esto es exactamente lo que se cambiará):\n- **Reunión:** Prueba HermesHQ lote 1\n"
    "- **Lugar:** Norte (reservada como recurso: salanorte@contoso.cl)\n\n¿Confirmas? Responde «sí» para "
    "aplicar el cambio."
)
_NOMBRES = {
    "create": "ms365_calendar_create_event",
    "update": "ms365_calendar_update_event",
    "delete": "ms365_calendar_delete_event",
}


def _load():
    spec = importlib.util.spec_from_file_location("calendario_directo", PLUGIN_PATH)
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
        self.escrituras = []
        self.error_graph = None

    def _graph(self, method, path, token, payload=None):
        if method == "GET":
            return dict(EVENTO)
        self.escrituras.append((method, path, payload))
        if self.error_graph:
            return {"error": self.error_graph}
        if method == "DELETE":
            return {}
        return {**EVENTO, **(payload or {}), "start": EVENTO["start"]}

    def _con(self, tarea, mensaje, anterior="", conv="s1", metadata=None):
        payload = {
            "task_id": tarea,
            "mensaje": mensaje,
            "respuesta_anterior": anterior,
            "metadata": {"public_session_id": conv, **(metadata or {})},
        }
        return (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
        )

    def _en(self, ctx, funcion, *args):
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4]:
            return funcion(*args)

    def _vista(self, herramienta, args, tarea="t1", conv="s1"):
        funcion = {
            "create": self.mod._create_event_tool,
            "update": self.mod._update_event_tool,
            "delete": self.mod._delete_event_tool,
        }[herramienta]
        out = json.loads(self._en(self._con(tarea, "hazlo", conv=conv), funcion, args))
        assert out.get("requiere_aprobacion"), out
        self.vista_mostrada = runner.texto_vista_calendario(_NOMBRES[herramienta], out["vista_previa"])
        runner._marcar_vista_mostrada({"task_id": tarea}, self.vista_mostrada)
        return out

    def _confirmar(self, mensaje="sí", tarea="t2", anterior=None, conv="s1", metadata=None):
        anterior = self.vista_mostrada + "\n\n⚠️ aviso agregado después" if anterior is None else anterior
        return self._en(self._con(tarea, mensaje, anterior, conv, metadata), self.mod.ejecutar_aprobacion_pendiente)


class TestEjecutaLoAprobado(_Base):
    def test_crear_con_un_solo_si_y_sin_duplicar(self):
        args = {
            "subject": "Prueba HermesHQ lote 1",
            "start": "2026-11-12T15:00:00",
            "end": "2026-11-12T15:30:00",
            "timezone": "America/Santiago",
        }
        self._vista("create", args)
        hecho = self._confirmar()
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual([e[0] for e in self.escrituras], ["POST"])
        self.assertIsNone(self._confirmar(tarea="t3"))
        self.assertEqual(len(self.escrituras), 1)

    def test_caso_real_mover_con_si(self):
        self._vista(
            "update",
            {
                "event_id": "ev-real-1",
                "start": "2026-11-12T16:00:00",
                "end": "2026-11-12T16:30:00",
                "timezone": "America/Santiago",
            },
        )
        hecho = self._confirmar()
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual(hecho["herramienta"], "ms365_calendar_update_event")
        self.assertEqual([e[:2] for e in self.escrituras], [("PATCH", "/me/events/ev-real-1")])
        self.assertEqual(self.escrituras[0][2]["start"]["dateTime"], "2026-11-12T16:00:00")

    def test_caso_real_bateria_la_vista_del_servidor_para_mover_basta_para_ejecutar_el_si(self):
        out = self._vista(
            "update",
            {
                "event_id": "ev-real-1",
                "start": "2026-11-12T16:00:00",
                "end": "2026-11-12T16:30:00",
                "timezone": "America/Santiago",
            },
        )
        vista_servidor = runner.texto_vista_calendario("ms365_calendar_update_event", out["vista_previa"])
        self.assertIn("Prueba HermesHQ lote 1", vista_servidor)
        hecho = self._confirmar(anterior=vista_servidor)
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual([e[:2] for e in self.escrituras], [("PATCH", "/me/events/ev-real-1")])

    def test_caso_real_cancelar_con_si_eliminala(self):
        self._vista("delete", {"event_id": "ev-real-1"})
        hecho = self._confirmar("Sí, elimínala")
        self.assertTrue(hecho["resultado"]["success"], hecho)
        self.assertEqual([e[:2] for e in self.escrituras], [("DELETE", "/me/events/ev-real-1")])

    def test_error_de_graph_se_devuelve_tal_cual_y_no_se_reintenta(self):
        self._vista("delete", {"event_id": "ev-real-1"})
        self.error_graph = "Error 503: servicio no disponible"
        hecho = self._confirmar()
        self.assertFalse(hecho["resultado"]["success"])
        self.assertIn("503", hecho["resultado"]["error"])
        self.assertIsNone(self._confirmar(tarea="t3"))
        self.assertEqual(len(self.escrituras), 1)


class TestNoEjecutaSinConfirmacionValida(_Base):
    def setUp(self):
        super().setUp()
        self._vista("delete", {"event_id": "ev-real-1"})

    def _nada(self, **kw):
        self.assertIsNone(self._confirmar(**kw))
        self.assertEqual(self.escrituras, [])

    def test_mensaje_que_no_es_confirmacion_pura(self):
        for mensaje in ("sí pero mejor el viernes", "¿sí?", "no", "espera", "cámbiala a las 17"):
            with self.subTest(mensaje=mensaje):
                self._nada(mensaje=mensaje)

    def test_otra_conversacion(self):
        self._nada(conv="otra-sesion")

    def test_misma_tarea_que_la_vista_previa(self):
        self._nada(tarea="t1")

    def test_el_usuario_no_vio_la_vista_previa(self):
        self._nada(anterior="Te propongo revisar tu agenda. ¿Seguimos?")

    def test_caso_real_otra_vista_previa_escrita_a_mano_con_el_mismo_asunto(self):
        self._nada(anterior=VISTA_A_MANO)

    def test_vista_registrada_que_el_runner_no_mostro(self):
        ruta = Path(self.tmp.name) / "work" / ".calendario_pendiente.json"
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        datos.pop("mostrada")
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        self._nada()

    def test_tarea_automatica(self):
        self._nada(metadata={"scheduled": True})

    def test_pendiente_vencido(self):
        ruta = Path(self.tmp.name) / "work" / ".calendario_pendiente.json"
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        datos["ts"] = time.time() - 5 * 3600
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        self._nada()


class TestRunner(unittest.TestCase):
    def _ejecucion(self, resultado, herramienta="ms365_calendar_update_event"):
        return {
            "herramienta": herramienta,
            "args": {"event_id": "ev-real-1", "aprobado_por_el_usuario": True},
            "resultado": resultado,
            "preview": {
                "evento_actual": {
                    "asunto": "Prueba HermesHQ lote 1",
                    "inicio": "2026-11-12T15:00:00",
                    "zona_horaria": "America/Santiago",
                }
            },
        }

    def _directo(self, ejecucion):
        plugin = SimpleNamespace(ejecutar_aprobacion_pendiente=lambda: ejecucion)
        payload = {"task_id": "t2", "hermes_home": "/x", "prompt": "sí", "metadata": {"source": "public_chat"}}
        with (
            patch.object(runner, "_cargar_plugin_calendario", return_value=plugin),
            patch.object(runner, "_chat_con_usuario", return_value=True),
        ):
            return runner._calendario_aprobado_directo(payload)

    def test_exito_confirmado_por_microsoft_365(self):
        evento = {**EVENTO, "start": {"dateTime": "2026-11-12T16:00:00", "timeZone": "America/Santiago"}}
        res = self._directo(self._ejecucion({"success": True, "event": evento}))
        self.assertTrue(res["final_response"].startswith("✅ Reunión actualizada"))
        self.assertIn("**Inicio:** jueves 12-11-2026 16:00 (hora de Chile)", res["final_response"])
        en_utc = {**EVENTO, "start": {"dateTime": "2026-11-12T17:00:00.0000000", "timeZone": "UTC"}}
        res_utc = self._directo(self._ejecucion({"success": True, "event": en_utc}))
        self.assertIn("jueves 12-11-2026 14:00 (hora de Chile)", res_utc["final_response"], "caso real: «17:00 (UTC)»")
        self.assertNotIn("UTC", res_utc["final_response"])
        self.assertEqual([c["name"] for c in res["tool_calls"]], ["ms365_calendar_update_event"])
        self.assertEqual(res["iterations"], 0)

    def test_fallo_se_informa_sin_afirmar_nada(self):
        res = self._directo(self._ejecucion({"success": False, "error": "Error 503"}, "ms365_calendar_delete_event"))
        self.assertTrue(res["final_response"].startswith("❌ No se pudo cancelar la reunión"))
        self.assertIn("No se hizo ningún cambio", res["final_response"])
        self.assertNotIn("✅", res["final_response"])

    def test_sin_aprobacion_valida_sigue_el_modelo(self):
        self.assertIsNone(self._directo(None))
        self.assertIsNone(self._directo(self._ejecucion({"success": False, "requiere_aprobacion": True})))

    def test_main_no_llama_al_modelo_cuando_la_plataforma_ejecuta(self):
        ejecucion = self._ejecucion({"success": True, "event": EVENTO}, "ms365_calendar_delete_event")
        eventos = []
        llamadas_al_modelo = []
        payload = {
            "task_id": "t2",
            "prompt": "sí",
            "cwd": tempfile.mkdtemp(),
            "hermes_home": "/x",
            "model": "m",
            "metadata": {"source": "public_chat"},
        }
        with (
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(runner, "_emit", side_effect=eventos.append),
            patch.object(runner, "_abrir_salida", return_value=None),
            patch.object(runner, "_proteger_proceso", return_value=None),
            patch.object(runner, "_ocultar_token_a_los_comandos", return_value=None),
            patch.object(runner, "_envio_aprobado_directo", return_value=None),
            patch.object(runner, "_chat_con_usuario", return_value=True),
            patch.object(
                runner,
                "_cargar_plugin_calendario",
                return_value=SimpleNamespace(ejecutar_aprobacion_pendiente=lambda: ejecucion),
            ),
            patch.dict(sys.modules, {"run_agent": SimpleNamespace(AIAgent=lambda **k: llamadas_al_modelo.append(k))}),
        ):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(llamadas_al_modelo, [])
        resultado = next(e for e in eventos if e.get("event") == "result")
        self.assertTrue(resultado["final_response"].startswith("✅ Reunión cancelada"))


if __name__ == "__main__":
    unittest.main()
