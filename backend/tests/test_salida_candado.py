import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_calendar_teams_meeting import _load as _load_cal
from tests.test_ms365_mail_get import _load as _load_mail
from tests.test_teams_send_to_person import _cargar as _load_teams


class _GateBase(unittest.TestCase):
    loader = None

    def setUp(self):
        self.mod = type(self).loader()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _graph(self, method, path, token, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/me/chats/C1?"):
            return {"id": "C1", "topic": "Equipo", "chatType": "group", "members": []}
        if method == "GET":
            return {
                "subject": "Reunión",
                "start": {"dateTime": "2026-10-30T12:30:00"},
                "attendees": [
                    {"emailAddress": {"address": "ana@x.cl"}},
                    {"emailAddress": {"address": "beto@x.cl"}},
                ],
            }
        return {"id": "E1"}

    def _call(self, tool, args, tarea, mensaje=None):
        if mensaje is None:
            mensaje = "sí" if args.get("aprobado_por_el_usuario") else "prepáralo"
        carga = json.dumps({"task_id": tarea, "metadata": {}, "mensaje": mensaje})
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
        ):
            return json.loads(tool(args))

    def _metodos(self, verbo):
        return [(m, p) for m, p, _ in self.calls if m == verbo]


class TestMailSendGate(_GateBase):
    loader = staticmethod(_load_mail)

    def test_preview_lists_bcc_and_sends_nothing(self):
        args = {"to": ["a@x.cl"], "cc": ["c@x.cl"], "bcc": ["oculto@x.cl"], "subject": "s", "body": "<p>hola</p>"}
        r = self._call(self.mod._send_email_tool, args, "t1")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["para"], ["a@x.cl"])
        self.assertEqual(r["vista_previa"]["cc"], ["c@x.cl"])
        self.assertEqual(r["vista_previa"]["cco"], ["oculto@x.cl"])
        self.assertEqual(self._metodos("POST"), [])

    def test_same_turn_approval_is_rejected(self):
        args = {"to": ["a@x.cl"], "subject": "s", "body": "b", "aprobado_por_el_usuario": True}
        r = self._call(self.mod._send_email_tool, args, "t1")
        self.assertFalse(r["success"])
        self.assertEqual(self._metodos("POST"), [])

    def test_cross_turn_approval_sends(self):
        base = {"to": ["a@x.cl"], "subject": "s", "body": "b"}
        self._call(self.mod._send_email_tool, base, "t1")
        self.calls.clear()
        r = self._call(self.mod._send_email_tool, {**base, "aprobado_por_el_usuario": True}, "t2")
        self.assertTrue(r["success"], r)
        self.assertIn(("POST", "/me/sendMail"), self._metodos("POST"))


class TestCalendarGate(_GateBase):
    loader = staticmethod(_load_cal)

    def test_create_preview_then_cross_turn(self):
        args = {"subject": "Reu", "start": "2026-10-30T12:30:00", "end": "2026-10-30T13:00:00", "attendees": ["x@y.cl"]}
        r = self._call(self.mod._create_event_tool, args, "t1")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["asistentes"], ["x@y.cl"])
        self.assertEqual(self._metodos("POST"), [])
        self.calls.clear()
        self._call(self.mod._create_event_tool, {**args, "aprobado_por_el_usuario": True}, "t2")
        self.assertIn(("POST", "/me/events"), self._metodos("POST"))

    def test_update_full_replace_shows_who_is_cancelled(self):
        args = {"event_id": "E1", "attendees": ["ana@x.cl"]}
        r = self._call(self.mod._update_event_tool, args, "t1")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["asistentes_QUITADOS_reciben_cancelacion"], ["beto@x.cl"])
        self.assertEqual(self._metodos("PATCH"), [])

    def test_delete_lists_cancellations_and_gates(self):
        r = self._call(self.mod._delete_event_tool, {"event_id": "E1"}, "t1")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertIn("ana@x.cl", r["vista_previa"]["asistentes_que_reciben_cancelacion"])
        self.assertEqual(self._metodos("DELETE"), [])
        self.calls.clear()
        r2 = self._call(self.mod._delete_event_tool, {"event_id": "E1", "aprobado_por_el_usuario": True}, "t2")
        self.assertTrue(r2["success"], r2)
        self.assertIn(("DELETE", "/me/events/E1"), self._metodos("DELETE"))


class TestTeamsChatGate(_GateBase):
    loader = staticmethod(_load_teams)

    def test_preview_then_cross_turn_posts(self):
        base = {"chat_id": "C1", "message": "hola"}
        r = self._call(self.mod._send_chat_message_tool, base, "t1")
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(r["vista_previa"]["chat_id"], "C1")
        self.assertEqual(self._metodos("POST"), [])
        self.calls.clear()
        r2 = self._call(self.mod._send_chat_message_tool, {**base, "aprobado_por_el_usuario": True}, "t2")
        self.assertTrue(r2["success"], r2)
        self.assertIn(("POST", "/me/chats/C1/messages"), self._metodos("POST"))

    def test_same_turn_rejected(self):
        r = self._call(
            self.mod._send_chat_message_tool, {"chat_id": "C1", "message": "x", "aprobado_por_el_usuario": True}, "t1"
        )
        self.assertFalse(r["success"])
        self.assertEqual(self._metodos("POST"), [])


class TestHonestyRule(unittest.TestCase):
    def test_platform_rules_extend_honesty_to_all_outbound_actions(self):
        from hermeshq.services.hermes_runtime import PLATFORM_RULES

        for fragmento in (
            "Honestidad en TODA acción de salida",
            "no afirmes que agendaste",
            "en la misma respuesta en que generaste la vista previa",
        ):
            self.assertIn(fragmento, PLATFORM_RULES)

    def test_platform_rules_forbid_inventing_causes_and_point_to_respond_tool(self):
        from hermeshq.services.hermes_runtime import PLATFORM_RULES

        for fragmento in (
            "No inventes causas ni explicaciones",
            "eventMessageRequest",
            "ms365_calendar_respond_event",
        ):
            self.assertIn(fragmento, PLATFORM_RULES)

    def test_platform_rules_block_fabricated_content_and_loose_memory(self):
        from hermeshq.services.hermes_runtime import PLATFORM_RULES

        for fragmento in (
            "No fabriques contenido que el usuario no pidió",
            "no saltes a otro tema ni generes un entregable distinto",
            "guarda en memoria solo lo que el usuario te pida recordar explícitamente",
            "una dirección que contradiga la cuenta Microsoft 365 verificada",
        ):
            self.assertIn(fragmento, PLATFORM_RULES)


if __name__ == "__main__":
    unittest.main()
