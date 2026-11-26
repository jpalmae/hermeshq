import importlib.util
import json
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
A_LAS_12_08 = datetime(2026, 11, 12, 15, 8, tzinfo=UTC)


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_estado_real", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _evento(asunto, inicio, fin, dia_completo=False, **extra):
    return {
        "id": f"id-{asunto[:6]}",
        "subject": asunto,
        "isAllDay": dia_completo,
        "start": {"dateTime": inicio, "timeZone": "UTC"},
        "end": {"dateTime": fin, "timeZone": "UTC"},
        **extra,
    }


EVENTOS = [
    _evento(
        "Revisión de Propuesta Comercial",
        "2026-11-02T00:00:00",
        "2026-11-12T00:00:00",
        dia_completo=True,
        responseStatus={"response": "notResponded"},
    ),
    _evento("9.3 Revisar orígenes", "2026-11-09T00:00:00", "2026-11-19T00:00:00", dia_completo=True),
    _evento(
        "Revisión Propuesta SpBox",
        "2026-11-12T12:30:00.0000000",
        "2026-11-12T13:00:00.0000000",
        isOrganizer=True,
        responseStatus={"response": "organizer"},
    ),
    _evento(
        "Curso | Ley Karin",
        "2026-11-12T14:00:00.0000000",
        "2026-11-12T15:00:00.0000000",
        responseStatus={"response": "notResponded"},
    ),
    _evento(
        "Dudas SUC4",
        "2026-11-12T18:00:00.0000000",
        "2026-11-12T18:30:00.0000000",
        responseStatus={"response": "accepted"},
    ),
    _evento(
        "Café de Media Tarde",
        "2026-11-12T19:30:00.0000000",
        "2026-11-12T20:30:00.0000000",
        isCancelled=True,
        responseStatus={"response": "tentativelyAccepted"},
    ),
]


class TestEstadoReal(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.rutas = []

    def _listar(self):
        def graph(method, path, token, payload=None):
            self.rutas.append(path)
            return {"value": EVENTOS}

        real = self.mod._estado_del_evento
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_guardar_listado", return_value=None),
            patch.object(self.mod, "_estado_del_evento", side_effect=lambda o, c, z: real(o, c, z, A_LAS_12_08)),
        ):
            return json.loads(
                self.mod._list_events_tool({"start": "2026-11-12T03:00:00Z", "end": "2026-11-13T03:00:00Z"})
            )

    def test_caso_real_a_las_12_08(self):
        r = self._listar()
        self.assertTrue(r["success"], r)
        estado = {e["subject"]: (e.get("momento"), e.get("tu_respuesta")) for e in r["events"]}
        self.assertEqual(estado["Revisión Propuesta SpBox"], ("terminó", "organizas tú"))
        self.assertEqual(estado["Curso | Ley Karin"], ("terminó", "sin responder"))
        self.assertEqual(estado["Dudas SUC4"], ("próxima", "aceptada"))
        self.assertEqual(estado["Café de Media Tarde"], ("cancelada", "provisional"))
        self.assertEqual(estado["Revisión de Propuesta Comercial"][0], "terminó", "terminó a las 00:00 de hoy")
        self.assertEqual(estado["9.3 Revisar orígenes"], ("en curso", None), "sin respuesta en Graph: no se inventa")
        self.assertIn("no inventes otro estado", r["estado"])
        self.assertIn("responseStatus", self.rutas[0], "se pide a Graph")
        self.assertNotIn("responseStatus", json.dumps(r["events"]), "el dato crudo no se reenvía")

    def test_en_curso_justo_dentro_de_la_reunion(self):
        en_la_reunion = datetime(2026, 11, 12, 14, 30, tzinfo=UTC)
        convertido = self.mod._evento_en_zona(EVENTOS[3], "America/Santiago")
        estado = self.mod._estado_del_evento(EVENTOS[3], convertido, "America/Santiago", en_la_reunion)
        self.assertEqual(estado["momento"], "en curso")
