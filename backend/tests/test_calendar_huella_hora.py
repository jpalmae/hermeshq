import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ms365_calendar_huella_hora", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHuellaConHoraEquivalente(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _llamar(self, args, tarea, mensaje):
        def graph(method, path, token, payload=None):
            self.calls.append((method, path, payload))
            return {"id": "ev-1", "webLink": "https://outlook/ev-1"}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})}),
        ):
            return json.loads(self.mod._create_event_tool(args))

    def test_la_confirmacion_con_la_hora_sin_desfase_crea_el_evento(self):
        base = {"subject": "Prueba HermesHQ", "timezone": "America/Santiago"}
        vista = self._llamar(
            {**base, "start": "2026-11-11T15:00:00-03:00", "end": "2026-11-11T15:30:00-03:00"}, "t1", "agéndame"
        )
        self.assertTrue(vista["requiere_aprobacion"])
        self.assertEqual(vista["vista_previa"]["inicio"]["dateTime"], "2026-11-11T15:00:00")
        hecho = self._llamar(
            {**base, "start": "2026-11-11T15:00:00", "end": "2026-11-11T15:30:00", "aprobado_por_el_usuario": True},
            "t2",
            "sí",
        )
        self.assertTrue(hecho.get("success"), hecho)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["start"], {"dateTime": "2026-11-11T15:00:00", "timeZone": "America/Santiago"})

    def test_otra_hora_sigue_siendo_otra_vista_previa(self):
        base = {"subject": "Prueba HermesHQ", "timezone": "America/Santiago"}
        self._llamar({**base, "start": "2026-11-11T15:00:00-03:00", "end": "2026-11-11T15:30:00-03:00"}, "t1", "x")
        otra = self._llamar(
            {**base, "start": "2026-11-11T16:00:00", "end": "2026-11-11T16:30:00", "aprobado_por_el_usuario": True},
            "t2",
            "sí",
        )
        self.assertFalse(otra.get("success"))
        self.assertFalse([c for c in self.calls if c[0] == "POST"])

    def test_hora_en_utc_se_lleva_a_la_zona_del_evento(self):
        self.assertEqual(self.mod._hora_local("2026-11-11T18:00:00Z", "America/Santiago"), "2026-11-11T15:00:00")
        self.assertEqual(self.mod._hora_local("no es fecha", "America/Santiago"), "no es fecha")


class TestListadoEnHoraLocal(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _listar(self, args, eventos):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", return_value={"value": eventos}),
        ):
            return json.loads(self.mod._list_events_tool(args))

    def test_caso_real_18_utc_se_entrega_como_15_en_chile(self):
        evento = {
            "subject": "Prueba HermesHQ lote 1",
            "isAllDay": False,
            "start": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-12T18:30:00.0000000", "timeZone": "UTC"},
        }
        out = self._listar({"start": "2026-11-12T00:00:00-03:00", "end": "2026-11-12T23:59:59-03:00"}, [evento])
        self.assertEqual(out["zona_horaria"], "America/Santiago")
        self.assertEqual(out["events"][0]["start"], {"dateTime": "2026-11-12T15:00:00", "timeZone": "America/Santiago"})
        self.assertEqual(out["events"][0]["end"], {"dateTime": "2026-11-12T15:30:00", "timeZone": "America/Santiago"})

    def test_zona_pedida_y_horario_de_invierno(self):
        evento = {"start": {"dateTime": "2026-07-01T14:00:00.0000000", "timeZone": "UTC"}}
        out = self._listar({"timezone": "America/Santiago"}, [evento])
        self.assertEqual(out["events"][0]["start"]["dateTime"], "2026-07-01T10:00:00")
        out = self._listar({"timezone": "Europe/Madrid"}, [evento])
        self.assertEqual(out["events"][0]["start"], {"dateTime": "2026-07-01T16:00:00", "timeZone": "Europe/Madrid"})

    def test_dia_completo_no_se_corre_de_dia(self):
        evento = {
            "isAllDay": True,
            "start": {"dateTime": "2026-11-12T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-13T00:00:00.0000000", "timeZone": "UTC"},
        }
        out = self._listar({}, [evento])
        self.assertEqual(out["events"][0]["start"]["dateTime"], "2026-11-12T00:00:00")
        self.assertEqual(out["events"][0]["end"]["dateTime"], "2026-11-13T00:00:00")

    def test_zona_invalida_usa_chile_y_datos_raros_no_rompen(self):
        out = self._listar({"timezone": "Marte/Base"}, [{"subject": "sin horas"}, "texto"])
        self.assertTrue(out["success"])
        self.assertEqual(out["zona_horaria"], "America/Santiago")
        self.assertEqual(out["events"], [{"subject": "sin horas", "ref": "E1"}, "texto"])

    def test_detalle_de_un_evento_tambien_en_hora_local(self):
        evento = {"id": "e1", "start": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"}}
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", return_value=evento),
        ):
            out = json.loads(self.mod._get_event_tool({"event_id": "e1"}))
        self.assertEqual(out["event"]["start"]["dateTime"], "2026-11-12T15:00:00")


class TestIdInventadoNoPideAprobacion(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _llamar(self, herramienta, args, existe=True, tarea="t1", mensaje="x"):
        def graph(method, path, token, payload=None):
            self.calls.append((method, path))
            if method == "GET":
                if not existe:
                    return {
                        "error": "Error 404: The specified object was not found in the store.",
                        "_graph_code": "ErrorItemNotFound",
                    }
                return {
                    "subject": "Prueba HermesHQ lote 1",
                    "isAllDay": False,
                    "start": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-11-12T18:30:00.0000000", "timeZone": "UTC"},
                    "attendees": [],
                }
            return {"id": "ev-1"}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})}),
        ):
            return json.loads(getattr(self.mod, herramienta)(args))

    def test_eliminar_con_id_inventado_falla_sin_pedir_aprobacion(self):
        out = self._llamar(
            "_delete_event_tool", {"event_id": "AAMkAGP5ADFfGAn6SDF6FgraUKFgFZ38GqT6SAreT5FeT5FeT5FeTn=="}, existe=False
        )
        self.assertFalse(out["success"])
        self.assertNotIn("requiere_aprobacion", out)
        self.assertIn("ms365_calendar_list_events", out["error"])
        self.assertNotIn("DELETE", [c[0] for c in self.calls])

    def test_modificar_con_id_inventado_falla_sin_pedir_aprobacion(self):
        out = self._llamar(
            "_update_event_tool",
            {
                "event_id": "inventado",
                "start": "2026-11-12T16:00:00",
                "end": "2026-11-12T16:30:00",
                "timezone": "America/Santiago",
            },
            existe=False,
        )
        self.assertFalse(out["success"])
        self.assertNotIn("requiere_aprobacion", out)
        self.assertNotIn("PATCH", [c[0] for c in self.calls])

    def test_vista_previa_muestra_el_evento_actual_en_hora_local(self):
        out = self._llamar("_delete_event_tool", {"event_id": "ev-1"})
        self.assertTrue(out["requiere_aprobacion"])
        actual = out["vista_previa"]["evento_actual"]
        self.assertEqual(actual["asunto"], "Prueba HermesHQ lote 1")
        self.assertEqual(actual["inicio"], "2026-11-12T15:00:00")
        self.assertEqual(actual["zona_horaria"], "America/Santiago")
        out = self._llamar(
            "_update_event_tool",
            {
                "event_id": "ev-1",
                "start": "2026-11-12T16:00:00",
                "end": "2026-11-12T16:30:00",
                "timezone": "America/Santiago",
            },
        )
        self.assertEqual(out["vista_previa"]["evento_actual"]["inicio"], "2026-11-12T15:00:00")

    def test_la_ejecucion_aprobada_no_agrega_consultas(self):
        args = {
            "event_id": "ev-1",
            "start": "2026-11-12T16:00:00",
            "end": "2026-11-12T16:30:00",
            "timezone": "America/Santiago",
        }
        self._llamar("_update_event_tool", args, tarea="t1", mensaje="muévela")
        self.calls.clear()
        out = self._llamar("_update_event_tool", {**args, "aprobado_por_el_usuario": True}, tarea="t2", mensaje="sí")
        self.assertTrue(out.get("success"), out)
        self.assertEqual([c[0] for c in self.calls], ["PATCH"])


class TestReferenciasCortasDeEventos(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _llamar(self, herramienta, args, conv="s1", tarea="t1", mensaje="x"):
        ids = ["AAMkAGMyMmYy-largo-real-1==", "AAMkAGMyMmYy-largo-real-2=="]

        def graph(method, path, token, payload=None):
            self.calls.append((method, path))
            if path.startswith("/me/calendarView"):
                return {
                    "value": [
                        {
                            "id": i,
                            "subject": f"Evento {n}",
                            "isAllDay": False,
                            "start": {"dateTime": "2026-11-12T18:00:00.0000000", "timeZone": "UTC"},
                            "end": {"dateTime": "2026-11-12T18:30:00.0000000", "timeZone": "UTC"},
                        }
                        for n, i in enumerate(ids, start=1)
                    ]
                }
            if method == "GET":
                return {"subject": "Evento", "start": {"dateTime": "2026-11-12T18:00:00", "timeZone": "UTC"}}
            return {}

        payload = {"task_id": tarea, "mensaje": mensaje, "metadata": {"public_session_id": conv}}
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
        ):
            return json.loads(getattr(self.mod, herramienta)(args))

    def test_el_listado_entrega_refs_y_cancelar_con_e2_usa_el_id_real(self):
        listado = self._llamar("_list_events_tool", {}, tarea="t2")
        self.assertEqual([e["ref"] for e in listado["events"]], ["E1", "E2"])
        self.assertIn("ref", listado["como_referirse"])
        out = self._llamar("_delete_event_tool", {"event_id": "E2"}, tarea="t2")
        self.assertTrue(out["requiere_aprobacion"], out)
        self.assertEqual(out["vista_previa"]["event_id"], "AAMkAGMyMmYy-largo-real-2==")
        self.assertIn(
            ("GET", "/me/events/AAMkAGMyMmYy-largo-real-2==?$select=subject,start,end,isAllDay,attendees"), self.calls
        )

    def test_mover_con_ref_y_minusculas_o_almohadilla(self):
        self._llamar("_list_events_tool", {}, tarea="t2")
        for ref in ("e1", "#E1"):
            out = self._llamar(
                "_update_event_tool",
                {
                    "event_id": ref,
                    "start": "2026-11-12T16:00:00",
                    "end": "2026-11-12T16:30:00",
                    "timezone": "America/Santiago",
                },
                tarea="t2",
            )
            self.assertEqual(out["vista_previa"]["event_id"], "AAMkAGMyMmYy-largo-real-1==", ref)

    def test_caso_real_ref_de_un_mensaje_anterior_se_rechaza_y_dice_cual_era(self):
        self._llamar("_list_events_tool", {}, tarea="t1")
        self.calls.clear()
        out = self._llamar("_update_event_tool", {"event_id": "E1", "location": "Sala Sur"}, tarea="t2")
        self.assertFalse(out["success"])
        self.assertIn("Evento 1", out["error"])
        self.assertIn("buscar", out["error"])
        self.assertFalse([c for c in self.calls if "largo-real-1" in c[1]], "no toca la reunión adivinada")

    def test_ref_de_un_mensaje_anterior_vale_si_confirma_la_accion_pendiente(self):
        self._llamar("_list_events_tool", {}, tarea="t1")
        previa = self._llamar("_delete_event_tool", {"event_id": "E2"}, tarea="t1")
        self.assertTrue(previa["requiere_aprobacion"], previa)
        otra = self._llamar("_delete_event_tool", {"event_id": "E1"}, tarea="t2", mensaje="sí")
        self.assertFalse(otra.get("success", True))
        self.assertIn("Evento 1", otra["error"])
        misma = self._llamar("_delete_event_tool", {"event_id": "E2"}, tarea="t2", mensaje="sí")
        self.assertNotIn("mensaje anterior", str(misma.get("error") or ""))

    def test_ref_de_otra_conversacion_o_inexistente_no_se_traduce(self):
        self._llamar("_list_events_tool", {})
        self.calls.clear()
        for args, conv in (({"event_id": "E1"}, "otra"), ({"event_id": "E9"}, "s1")):
            self._llamar("_delete_event_tool", args, conv=conv, tarea="t2")
        self.assertNotIn("AAMkAGMyMmYy-largo-real-1==", " ".join(c[1] for c in self.calls))
        self.assertFalse([c for c in self.calls if c[0] == "DELETE"])

    def test_un_id_completo_sigue_funcionando(self):
        out = self._llamar("_delete_event_tool", {"event_id": "AAMkAGMyMmYy-largo-real-1=="})
        self.assertTrue(out["requiere_aprobacion"], out)


if __name__ == "__main__":
    unittest.main()
