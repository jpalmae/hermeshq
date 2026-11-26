import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermeshq.scripts import hermes_task_runner as runner

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)
EVENTO = {
    "id": "ev-hermeshq",
    "subject": "Prueba HermesHQ",
    "start": {"dateTime": "2026-11-12T17:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-12T17:30:00.0000000", "timeZone": "UTC"},
    "attendees": [],
}


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_horas_sin_zona", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.escrituras = []

    def _graph(self, method, path, token, payload=None):
        if method != "GET":
            self.escrituras.append((method, path, payload))
            return {"id": "ev-hermeshq", "webLink": "https://outlook/ev"}
        return EVENTO

    def _llamar(self, herramienta, args):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_current_task_id", return_value="t-1"),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t-1", "mensaje": "amplía"})}),
        ):
            return json.loads(getattr(self.mod, herramienta)(args))


class TestHorasSinZona(_Base):
    def test_caso_real_el_fin_sin_zona_es_hora_de_chile(self):
        r = self._llamar("_update_event_tool", {"event_id": "ev-hermeshq", "end": "2026-11-12T15:00:00"})
        self.assertTrue(r.get("requiere_aprobacion"), r)
        self.assertEqual(
            r["vista_previa"]["nuevo_fin"], {"dateTime": "2026-11-12T15:00:00", "timeZone": "America/Santiago"}
        )
        texto = runner.texto_vista_calendario("ms365_calendar_update_event", r["vista_previa"])
        self.assertIn("- **Nuevo fin:** jueves 12-11-2026 15:00", texto)
        self.assertEqual(self.escrituras, [])

    def test_un_fin_antes_del_inicio_se_rechaza(self):
        r = self._llamar(
            "_update_event_tool",
            {"event_id": "ev-hermeshq", "end": "2026-11-12T12:00:00", "timezone": "America/Santiago"},
        )
        self.assertFalse(r["success"])
        self.assertIn("antes", r["error"])
        self.assertNotIn("requiere_aprobacion", r)

    def test_crear_con_fin_antes_del_inicio_se_rechaza(self):
        r = self._llamar(
            "_create_event_tool",
            {"subject": "Prueba", "start": "2026-11-12T14:00:00", "end": "2026-11-12T13:30:00"},
        )
        self.assertFalse(r["success"])
        self.assertIn("antes", r["error"])

    def test_con_desfase_explicito_sigue_igual(self):
        r = self._llamar(
            "_create_event_tool",
            {"subject": "Prueba", "start": "2026-11-12T17:00:00Z", "end": "2026-11-12T17:30:00Z"},
        )
        self.assertEqual(
            r["vista_previa"]["inicio"], {"dateTime": "2026-11-12T14:00:00", "timeZone": "America/Santiago"}
        )


class TestLugarNoEsSala(unittest.TestCase):
    def test_caso_real_el_lugar_dice_que_no_reserva_la_sala(self):
        texto = runner.texto_vista_calendario(
            "ms365_calendar_update_event",
            {"nueva_ubicacion": "Norte", "evento_actual": {"asunto": "Prueba HermesHQ"}},
        )
        self.assertIn("- **Nuevo lugar:** Norte (texto del lugar: no reserva la sala)", texto)
