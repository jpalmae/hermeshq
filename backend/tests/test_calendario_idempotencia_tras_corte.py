import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

RUTA = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)
ARGS = {
    "subject": "Reunión AP5",
    "start": "2026-11-20T10:00:00",
    "end": "2026-11-20T10:30:00",
    "timezone": "America/Santiago",
}


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_idempotencia", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestCitaTrasCorte(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.posts = []
        self.eventos = []
        self.fallo_post = None
        self.fallo_get = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _graph(self, method, path, token, payload=None):
        if method == "POST":
            self.posts.append(payload)
            if self.fallo_post == "aceptado":
                self.eventos.append(
                    {
                        "id": "e1",
                        "subject": payload["subject"],
                        "createdDateTime": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    }
                )
                raise TimeoutError("timed out")
            if self.fallo_post == "perdido":
                raise TimeoutError("timed out")
            if self.fallo_post == "http_500":
                return {"error": "Error 500: boom"}
            return {"id": "ev-1", "webLink": "https://outlook/ev-1"}
        if method == "GET" and "/me/events" in path:
            return {"error": "Error 503: no"} if self.fallo_get else {"value": list(self.eventos)}
        return {}

    def _turno(self, tarea, mensaje, **extra):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_modo_automatico", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})}),
        ):
            try:
                return json.loads(self.mod._create_event_tool({**ARGS, **extra}))
            except TimeoutError:
                return {"excepcion": "timeout"}

    def test_timeout_con_graph_que_creo_el_reintento_no_duplica(self):
        self._turno("t1", "agéndame")
        self.fallo_post = "aceptado"
        self.assertEqual(self._turno("t2", "sí", aprobado_por_el_usuario=True), {"excepcion": "timeout"})
        self.fallo_post = None
        r = self._turno("t3", "sí", aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and r["ya_creado"], r)
        self.assertEqual(len(self.posts), 1)

    def test_control_negativo_timeout_sin_alta_el_reintento_crea_una_vez(self):
        self._turno("t1", "agéndame")
        self.fallo_post = "perdido"
        self._turno("t2", "sí", aprobado_por_el_usuario=True)
        self.fallo_post = None
        r = self._turno("t3", "sí", aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and not r.get("ya_creado"), r)
        self.assertEqual(len(self.posts), 2)

    def test_sin_poder_revisar_el_calendario_no_crea(self):
        self._turno("t1", "agéndame")
        self.fallo_post = "aceptado"
        self._turno("t2", "sí", aprobado_por_el_usuario=True)
        self.fallo_post = None
        self.fallo_get = True
        r = self._turno("t3", "sí", aprobado_por_el_usuario=True)
        self.assertFalse(r["success"])
        self.assertIn("No la creo otra vez", r["error"])
        self.assertEqual(len(self.posts), 1)

    def test_rechazo_firme_permite_reintentar(self):
        self._turno("t1", "agéndame")
        self.fallo_post = "http_500"
        self.assertFalse(self._turno("t2", "sí", aprobado_por_el_usuario=True)["success"])
        self.fallo_post = None
        r = self._turno("t3", "sí", aprobado_por_el_usuario=True)
        self.assertTrue(r["success"] and not r.get("ya_creado"), r)
        self.assertEqual(len(self.posts), 2)

    def test_alta_normal_no_consulta_eventos(self):
        self._turno("t1", "agéndame")
        r = self._turno("t2", "sí", aprobado_por_el_usuario=True)
        self.assertTrue(r["success"])
        self.assertEqual(len(self.posts), 1)
