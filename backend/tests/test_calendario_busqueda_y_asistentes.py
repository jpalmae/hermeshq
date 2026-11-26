import importlib.util
import json
import os
import re
import tempfile
import unittest
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


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_busqueda", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _evento(i, asunto, dia):
    return {
        "@odata.etag": 'W/"rXfV8ki/nEKt5r8NfMdePQAEQuWCAA=="',
        "id": f"AAMkADUx-{i}-" + "x" * 120,
        "subject": asunto,
        "bodyPreview": "Hola a todos " * 40,
        "isAllDay": False,
        "start": {"dateTime": f"{dia}T17:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{dia}T17:30:00.0000000", "timeZone": "UTC"},
    }


CALENDARIO = [_evento(i, f"Reunión semanal {i}", f"2026-{(i % 12) + 1:02d}-1{i % 9}") for i in range(200)]
CALENDARIO.append(_evento(999, "Prueba HermesHQ", "2026-11-12"))

INVITACION = {
    "id": "AAMkADUx-hermeshq",
    "subject": "Prueba HermesHQ",
    "isAllDay": False,
    "start": {"dateTime": "2026-11-12T17:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"},
    "isOrganizer": True,
    "responseStatus": {"response": "organizer"},
    "body": {
        "contentType": "html",
        "content": "<html><body>" + "<div>Únase a la reunión de Microsoft Teams</div>" * 300,
    },
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
    "attendees": [
        {
            "type": "required",
            "status": {"response": "none"},
            "emailAddress": {"name": "persona.externa@example.com", "address": "persona.externa@example.com"},
        },
        {
            "type": "optional",
            "status": {"response": "tentativelyAccepted"},
            "emailAddress": {"name": "Equipo Datos (Sede Central)", "address": "equipo-datos@contoso.cl"},
        },
        {
            "type": "required",
            "status": {"response": "accepted"},
            "emailAddress": {"name": "Julia Mora (Sede Central)", "address": "jmora@contoso.cl"},
        },
        {
            "type": "resource",
            "status": {"response": "accepted"},
            "emailAddress": {"name": "Sala Norte", "address": "salanorte2@contoso.cl"},
        },
    ],
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.rutas = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _graph(self, method, path, token, payload=None):
        self.rutas.append(path)
        if path.startswith("/me/events/"):
            return INVITACION
        desde = re.search(r"startDateTime=([0-9T:\-]+)Z", path).group(1)
        hasta = re.search(r"endDateTime=([0-9T:\-]+)Z", path).group(1)
        tope = int(re.search(r"\$top=(\d+)", path).group(1))
        dentro = [e for e in CALENDARIO if desde <= e["start"]["dateTime"][:19] < hasta]
        return {"value": dentro[:tope]}

    def _llamar(self, herramienta, args):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            return json.loads(getattr(self.mod, herramienta)(args))


class TestBuscarPorTitulo(_Base):
    def test_caso_real_una_llamada_encuentra_la_reunion_de_hoy(self):
        r = self._llamar(
            "_list_events_tool",
            {"buscar": "prueba hermeshq", "start": "2026-09-13T00:00:00Z", "end": "2027-03-12T00:00:00Z"},
        )
        self.assertTrue(r["success"], r)
        self.assertEqual([e["subject"] for e in r["events"]], ["Prueba HermesHQ"])
        self.assertEqual(r["events"][0]["ref"], "E1")
        self.assertIn("«prueba hermeshq»", r["busqueda"])
        self.assertLessEqual(len(self.rutas), 7, "de a 30 días en el servidor, no 10 páginas en el modelo")
        self.assertNotIn("@odata.etag", json.dumps(r))

    def test_sin_coincidencias_lo_dice_y_pide_no_recorrer(self):
        r = self._llamar(
            "_list_events_tool",
            {"buscar": "Comité de crisis", "start": "2026-10-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"},
        )
        self.assertEqual(r["events"], [])
        self.assertIn("no recorras", r["busqueda"])

    def test_sin_buscar_el_listado_no_cambia_y_se_aliviana(self):
        r = self._llamar(
            "_list_events_tool", {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z", "count": 50}
        )
        self.assertNotIn("busqueda", r)
        self.assertEqual(len(self.rutas), 1)
        self.assertTrue(all(len(e.get("bodyPreview") or "") <= 160 for e in r["events"]))


class TestQuienConfirmo(_Base):
    def test_caso_real_respuesta_de_cada_asistente_sin_el_html_de_teams(self):
        r = self._llamar("_get_event_tool", {"event_id": "AAMkADUx-hermeshq"})
        self.assertTrue(r["success"], r)
        evento = r["event"]
        respuestas = {a["nombre"]: (a["tipo"], a["respuesta"]) for a in evento["asistentes_y_respuestas"]}
        self.assertEqual(respuestas["Julia Mora (Sede Central)"], ("obligatorio", "aceptó"))
        self.assertEqual(respuestas["Equipo Datos (Sede Central)"], ("opcional", "provisional"))
        self.assertEqual(respuestas["persona.externa@example.com"], ("obligatorio", "sin responder"))
        self.assertEqual(respuestas["Sala Norte"], ("sala o recurso", "aceptó"))
        self.assertEqual(evento["tu_respuesta"], "organizas tú")
        self.assertLessEqual(len(evento["body"]["content"]), 800)
        self.assertNotIn("<div>", json.dumps(evento))
        self.assertEqual(evento["onlineMeeting"]["joinUrl"], "https://teams.microsoft.com/l/meetup-join/x")
