import json
import os
import unittest
from unittest.mock import patch

from tests.test_calendar_teams_meeting import _isolate_previews, _load


class TestResponderInvitacion(unittest.TestCase):
    EVENT_INFO = {
        "subject": "Revisión Iniciativas 2027",
        "start": {"dateTime": "2026-10-30T15:30:00.0000000"},
        "end": {"dateTime": "2026-10-30T16:30:00.0000000"},
        "organizer": {"emailAddress": {"address": "paula.rios@consultora.cl"}},
        "responseStatus": {"response": "notResponded"},
    }

    def setUp(self):
        self.mod = _load()
        self.calls = []
        _isolate_previews(self)

    def _graph_fn(self, message, event, post):
        info = event if event is not None else self.EVENT_INFO

        def graph(method, path, token, payload=None):
            self.calls.append((method, path, payload))
            if method == "GET" and "/messages/" in path:
                return message if message is not None else {}
            if method == "GET" and "/events/" in path:
                return info
            if method == "POST" and "/events/" in path:
                return post if post is not None else {}
            return {}

        return graph

    def _run(self, args, message=None, event=None, post=None):
        graph = self._graph_fn(message, event, post)

        def correr(a, actual):
            with (
                patch.object(self.mod, "_current_task_id", return_value=actual),
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": actual, "mensaje": "sí"})}),
            ):
                return json.loads(self.mod._respond_event_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            first = correr(args, "tarea-1")
            self.calls.clear()
            second = correr({**args, "aprobado_por_el_usuario": True}, "tarea-2")
            return first, second

    def _once(self, args, message=None, event=None, post=None):
        graph = self._graph_fn(message, event, post)
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value="tarea-1"),
        ):
            return json.loads(self.mod._respond_event_tool(args))

    def _posts(self):
        return [(p, pl) for m, p, pl in self.calls if m == "POST"]

    def test_accept_by_event_id_needs_two_turns_then_sends(self):
        first, second = self._run({"event_id": "E1", "respuesta": "aceptar"})
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertEqual(first["vista_previa"]["asunto"], "Revisión Iniciativas 2027")
        self.assertTrue(second["success"])
        self.assertEqual(self._posts(), [("/me/events/E1/accept", {"sendResponse": True})])

    def test_message_id_resuelve_a_la_cita(self):
        first, second = self._run(
            {"message_id": "MSG18", "respuesta": "aceptar"},
            message={"id": "MSG18", "event": {"id": "EVTX"}},
        )
        self.assertTrue(second["success"])
        self.assertEqual([p for p, _ in self._posts()], ["/me/events/EVTX/accept"])

    def test_tentativa_mapea_a_tentativelyAccept_con_comentario(self):
        _, second = self._run({"event_id": "E1", "respuesta": "tentativa", "comentario": "Quizá llego tarde"})
        self.assertTrue(second["success"])
        self.assertEqual(
            self._posts(), [("/me/events/E1/tentativelyAccept", {"sendResponse": True, "comment": "Quizá llego tarde"})]
        )

    def test_rechazar_sin_avisar_al_organizador(self):
        _, second = self._run({"event_id": "E1", "respuesta": "rechazar", "enviar_respuesta": False})
        self.assertTrue(second["success"])
        self.assertEqual(self._posts(), [("/me/events/E1/decline", {"sendResponse": False})])

    def test_respuesta_invalida_no_llama_a_graph(self):
        out = self._once({"event_id": "E1", "respuesta": "tal vez"})
        self.assertFalse(out["success"])
        self.assertIn("respuesta", out["error"])
        self.assertEqual(self.calls, [])

    def test_correo_sin_cita_asociada_se_reporta(self):
        out = self._once({"message_id": "MSG", "respuesta": "aceptar"}, message={"id": "MSG", "subject": "x"})
        self.assertFalse(out["success"])
        self.assertIn("cita", out["error"].lower())
        self.assertEqual(self._posts(), [])

    def test_sin_event_id_ni_message_id_pide_uno(self):
        out = self._once({"respuesta": "aceptar"})
        self.assertFalse(out["success"])
        self.assertIn("event_id", out["error"])


if __name__ == "__main__":
    unittest.main()


class TestCronNoAceptaInvitaciones(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.posts = []
        _isolate_previews(self)

    def _graph(self, method, path, token, payload=None):
        if method == "POST":
            self.posts.append(path)
        return TestResponderInvitacion.EVENT_INFO if method == "GET" else {}

    def test_en_una_tarea_programada_no_se_responde_ni_aprobando(self):
        carga = json.dumps({"task_id": "cron-1", "metadata": {"scheduled": True, "scheduled_task_id": "s1"}})
        for args in (
            {"event_id": "E1", "respuesta": "aceptar"},
            {
                "event_id": "E1",
                "respuesta": "aceptar",
                "aprobado_por_el_usuario": True,
                "comentario": "Confirmo asistencia.",
            },
        ):
            with (
                patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": carga, "HERMESHQ_APROBACION_CENTRAL": "0"}),
                patch.object(self.mod, "_task_user_id", return_value="u1"),
                patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
                patch.object(self.mod, "_graph", side_effect=self._graph),
            ):
                r = json.loads(self.mod._respond_event_tool(args))
            self.assertFalse(r.get("success"), r)
            self.assertIn("modo automático", r.get("error", ""))
        self.assertEqual(self.posts, [], "no se envió ninguna respuesta a la invitación")
