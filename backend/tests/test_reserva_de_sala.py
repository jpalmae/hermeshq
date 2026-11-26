import importlib.util
import json
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services import graph_proxy as gp

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)
SALAS = [
    {"displayName": "Sala Mirador Sur (Sede Central)", "mail": "salasur@contoso.cl"},
    {"displayName": "Sala Mirador Norte (Sede Central)", "mail": "salanorte@contoso.cl"},
    {"displayName": "Sur Turismo", "mail": "contacto@salasur.cl"},
]


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_salas", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.llamadas = []
        self.actuales = [{"emailAddress": {"address": "equipo-datos@contoso.cl"}, "type": "required"}]

    def _graph(self, metodo, ruta, token, payload=None, headers=None):
        self.llamadas.append((metodo, ruta, payload, headers))
        if ruta.startswith("/users?$search="):
            termino = urllib.parse.unquote(ruta.split("$search=", 1)[1].split("&", 1)[0])
            palabras = [p.split(":", 1)[1].strip('"').lower() for p in termino.split(" AND ")]
            return {"value": [s for s in SALAS if all(p in s["displayName"].lower() for p in palabras)]}
        if ruta.startswith("/users?$filter="):
            return {"value": [s for s in SALAS if s["mail"] in urllib.parse.unquote(ruta)]}
        if metodo == "GET" and "$select=attendees" in ruta:
            return {"attendees": self.actuales}
        if metodo == "GET":
            return {"subject": "Prueba HermesHQ", "start": {"dateTime": "2026-11-12T17:00:00", "timeZone": "UTC"}}
        return {"id": "evento-1", "subject": "Prueba ID 03", "webLink": "https://outlook/x"}

    def _llamar(self, herramienta, args, aprobar=True):
        from contextlib import ExitStack

        with ExitStack() as pila:
            pila.enter_context(patch.object(self.mod, "_task_user_id", return_value="u1"))
            pila.enter_context(patch.object(self.mod, "_get_m365_token", return_value=("tok", "")))
            pila.enter_context(patch.object(self.mod, "_graph", side_effect=self._graph))
            carga = json.dumps({"task_id": "t1", "mensaje": "x"})
            pila.enter_context(patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}))
            if aprobar:
                pila.enter_context(patch.object(self.mod, "_approval_gate", return_value=None))
            return json.loads(getattr(self.mod, herramienta)(args))

    def _escrito(self, metodo):
        return [p for m, _, p, _ in self.llamadas if m == metodo][0]


class TestCrearConSala(_Base):
    def test_caso_real_la_sala_va_como_recurso_y_como_lugar(self):
        args = {
            "subject": "Prueba ID 03",
            "start": "2026-11-12T14:00:00",
            "end": "2026-11-12T14:15:00",
            "attendees": ["equipo-datos@contoso.cl"],
            "sala": "Sala Mirador Sur",
        }
        salida = self._llamar("_create_event_tool", args)
        self.assertNotIn("error", salida)
        evento = self._escrito("POST")
        recursos = [a for a in evento["attendees"] if a["type"] == "resource"]
        self.assertEqual([r["emailAddress"]["address"] for r in recursos], ["salasur@contoso.cl"])
        self.assertEqual(evento["location"]["locationEmailAddress"], "salasur@contoso.cl")
        self.assertEqual(evento["location"]["locationType"], "conferenceRoom")
        self.assertIn("equipo-datos@contoso.cl", [a["emailAddress"]["address"] for a in evento["attendees"]])

    def test_la_vista_previa_muestra_la_sala_y_no_la_cuenta_como_invitado(self):
        args = {
            "subject": "Prueba",
            "start": "2026-11-12T14:00:00",
            "end": "2026-11-12T14:15:00",
            "sala": "Sur",
        }
        vista = self._llamar("_create_event_tool", args, aprobar=False)["vista_previa"]
        self.assertEqual(vista["sala"], "Sala Mirador Sur (Sede Central) (salasur@contoso.cl)")
        self.assertNotIn("salasur@contoso.cl", vista["asistentes"])
        self.assertIsNone(vista["ubicacion"])
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        self.assertIn("**Sala:** Sala Mirador Sur", texto)
        self.assertIn("se reserva como recurso", texto)
        self.assertNotIn("no reserva la sala", texto)
        self.assertIsNone(runner.sala_pedida_sin_incluir("crea una reunión en la Sala Mirador Sur", vista))

    def test_la_busqueda_pasa_por_el_broker_y_pide_consistencia(self):
        self._llamar(
            "_create_event_tool",
            {"subject": "x", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00", "sala": "Sala Mirador Sur"},
        )
        busqueda = [(r, h) for m, r, _, h in self.llamadas if r.startswith("/users?$search=")][0]
        self.assertEqual(busqueda[1], {"ConsistencyLevel": "eventual"})
        self.assertNotEqual(gp.clase_de("GET", busqueda[0]), "denegada")
        self.assertTrue(
            gp._RE_BUSQUEDA_DIRECTORIO.fullmatch(
                urllib.parse.unquote(busqueda[0].split("$search=", 1)[1].split("&")[0])
            )
        )

    def test_nombre_ambiguo_pregunta_sin_crear(self):
        salida = self._llamar(
            "_create_event_tool",
            {"subject": "x", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00", "sala": "Mirador"},
        )
        self.assertFalse(salida["success"])
        self.assertIn("Sala Mirador Sur", salida["error"])
        self.assertIn("Sala Mirador Norte", salida["error"])
        self.assertFalse([m for m, *_ in self.llamadas if m == "POST"])

    def test_sala_inexistente_no_queda_como_texto(self):
        salida = self._llamar(
            "_create_event_tool",
            {"subject": "x", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00", "sala": "Sala Neptuno"},
        )
        self.assertFalse(salida["success"])
        self.assertIn("No encontré la sala", salida["error"])
        self.assertFalse([m for m, *_ in self.llamadas if m == "POST"])

    def test_por_correo(self):
        self._llamar(
            "_create_event_tool",
            {
                "subject": "x",
                "start": "2026-11-12T14:00",
                "end": "2026-11-12T15:00",
                "sala": "salanorte@contoso.cl",
            },
        )
        evento = self._escrito("POST")
        self.assertEqual(evento["location"]["displayName"], "Sala Mirador Norte (Sede Central)")


class TestModificarConSala(_Base):
    def test_caso_real_agrega_la_sala_sin_quitar_invitados(self):
        args = {"event_id": "AAMkReal==", "sala": "sala norte"}
        vista = self._llamar("_update_event_tool", args, aprobar=False)["vista_previa"]
        self.assertEqual(vista["nueva_sala"], "Sala Mirador Norte (Sede Central) (salanorte@contoso.cl)")
        self.assertEqual(vista["asistentes_QUITADOS_reciben_cancelacion"], [])
        self.assertNotIn("salanorte@contoso.cl", vista["asistentes_agregados"])
        self.assertNotIn("nueva_ubicacion", vista)
        texto = runner.texto_vista_calendario("ms365_calendar_update_event", vista)
        self.assertIn("**Sala:** Sala Mirador Norte", texto)
        self.llamadas.clear()
        self._llamar("_update_event_tool", args)
        cambio = self._escrito("PATCH")
        self.assertEqual(
            [(a["emailAddress"]["address"], a["type"]) for a in cambio["attendees"]],
            [("equipo-datos@contoso.cl", "required"), ("salanorte@contoso.cl", "resource")],
        )
        self.assertEqual(cambio["location"]["locationEmailAddress"], "salanorte@contoso.cl")

    def test_si_ya_estaba_como_asistente_comun_pasa_a_recurso(self):
        self.actuales.append({"emailAddress": {"address": "salanorte@contoso.cl"}, "type": "required"})
        self._llamar("_update_event_tool", {"event_id": "AAMkReal==", "sala": "Norte"})
        cambio = self._escrito("PATCH")
        salas = [a for a in cambio["attendees"] if a["emailAddress"]["address"] == "salanorte@contoso.cl"]
        self.assertEqual([a["type"] for a in salas], ["resource"])

    def test_caso_real_cambiar_de_sala_libera_la_anterior(self):
        self.actuales.append(
            {
                "emailAddress": {"address": "salanorte@contoso.cl"},
                "type": "resource",
                "status": {"response": "accepted"},
            }
        )
        args = {"event_id": "AAMkReal==", "sala": "Sala Mirador Sur"}
        vista = self._llamar("_update_event_tool", args, aprobar=False)["vista_previa"]
        self.assertEqual(vista["asistentes_QUITADOS_reciben_cancelacion"], ["salanorte@contoso.cl"])
        self.llamadas.clear()
        self._llamar("_update_event_tool", args)
        cambio = self._escrito("PATCH")
        self.assertEqual(
            [(a["emailAddress"]["address"], a["type"]) for a in cambio["attendees"]],
            [("equipo-datos@contoso.cl", "required"), ("salasur@contoso.cl", "resource")],
        )
        self.assertEqual(cambio["location"]["locationEmailAddress"], "salasur@contoso.cl")

    def test_pedir_la_misma_sala_no_la_duplica_ni_la_quita(self):
        self.actuales.append({"emailAddress": {"address": "salanorte@contoso.cl"}, "type": "resource"})
        self._llamar("_update_event_tool", {"event_id": "AAMkReal==", "sala": "Sala Mirador Norte"})
        cambio = self._escrito("PATCH")
        salas = [a for a in cambio["attendees"] if a["emailAddress"]["address"] == "salanorte@contoso.cl"]
        self.assertEqual([a["type"] for a in salas], ["resource"])

    def test_cambiar_sala_no_toca_a_los_invitados_comunes(self):
        self.actuales.append({"emailAddress": {"address": "crojas@contoso.cl"}, "type": "required"})
        self.actuales.append({"emailAddress": {"address": "salanorte@contoso.cl"}, "type": "resource"})
        self._llamar("_update_event_tool", {"event_id": "AAMkReal==", "sala": "Sala Mirador Sur"})
        cambio = self._escrito("PATCH")
        direcciones = {a["emailAddress"]["address"] for a in cambio["attendees"]}
        self.assertEqual(direcciones, {"equipo-datos@contoso.cl", "crojas@contoso.cl", "salasur@contoso.cl"})

    def test_el_esquema_ofrece_sala_en_crear_y_modificar(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        self.mod.register(ctx)
        esquemas = {c.kwargs["name"]: c.kwargs["schema"] for c in ctx.register_tool.call_args_list}
        for nombre in ("ms365_calendar_create_event", "ms365_calendar_update_event"):
            self.assertIn("sala", esquemas[nombre]["parameters"]["properties"])
