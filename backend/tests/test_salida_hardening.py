import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_calendar_teams_meeting import _load as _load_cal
from tests.test_ms365_mail_get import _load as _load_mail
from tests.test_teams_send_to_person import _cargar as _load_teams

PAYLOAD_AUTO = json.dumps({"task_id": "t1", "metadata": {"scheduled": True, "thread_user_id": "u1"}})


class TestModoAutomaticoNiega(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name, "HERMESHQ_TASK_PAYLOAD": PAYLOAD_AUTO})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, mod, tool, args):
        self.calls = []

        def graph(method, path, token, payload=None, **kwargs):
            self.calls.append((method, path))
            return {"value": []}

        with (
            patch.object(mod, "_task_user_id", return_value="u1"),
            patch.object(mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(mod, "_graph", side_effect=graph),
        ):
            return json.loads(tool(args))

    def test_mail_send_is_denied_in_automatic_mode(self):
        mod = _load_mail()
        r = self._run(mod, mod._send_email_tool, {"to": ["a@x.cl"], "subject": "s", "body": "b"})
        self.assertFalse(r["success"])
        self.assertIn("modo automático", r["error"])
        self.assertNotIn(("POST", "/me/sendMail"), self.calls)

    def test_calendar_create_is_denied_in_automatic_mode(self):
        mod = _load_cal()
        r = self._run(
            mod, mod._create_event_tool, {"subject": "x", "start": "2026-10-30T12:30:00", "end": "2026-10-30T13:00:00"}
        )
        self.assertFalse(r["success"])
        self.assertIn("modo automático", r["error"])
        self.assertNotIn(("POST", "/me/events"), self.calls)

    def test_teams_chat_is_denied_in_automatic_mode(self):
        mod = _load_teams()
        r = self._run(mod, mod._send_chat_message_tool, {"chat_id": "C1", "message": "hola"})
        self.assertFalse(r["success"])
        self.assertIn("modo automático", r["error"])


class TestResolverDirectorio(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mail()

    def _resolver(self, correos, respuesta):
        def graph(method, path, token, payload=None, **kwargs):
            return respuesta

        with patch.object(self.mod, "_graph", side_effect=graph):
            return self.mod._resolver_directorio("tok", correos)

    def test_found_addresses_show_the_directory_name(self):
        etiquetas = self._resolver(
            ["ana@x.cl", "inventado@contoso.cl"],
            {"value": [{"mail": "ana@x.cl", "displayName": "Ana Pérez"}]},
        )
        self.assertEqual(etiquetas["ana@x.cl"], "✓ en directorio: Ana Pérez")
        self.assertIn("NO está en el directorio", etiquetas["inventado@contoso.cl"])

    def test_directory_query_failure_degrades_to_no_annotation(self):
        etiquetas = self._resolver(["a@x.cl"], {"error": "Error 403: insufficient privileges"})
        self.assertEqual(etiquetas, {})

    def test_empty_input(self):
        self.assertEqual(self.mod._resolver_directorio("tok", []), {})


class TestSendPreviewHasVerification(unittest.TestCase):
    def setUp(self):
        self.mod = _load_mail()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_preview_includes_directory_verification(self):
        def graph(method, path, token, payload=None, **kwargs):
            if path.startswith("/users"):
                return {"value": [{"mail": "a@x.cl", "displayName": "Ana"}]}
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value="t1"),
        ):
            r = json.loads(
                self.mod._send_email_tool({"to": ["a@x.cl", "fake@contoso.cl"], "subject": "s", "body": "b"})
            )
        self.assertTrue(r["requiere_aprobacion"])
        verif = r["vista_previa"]["verificacion_directorio"]
        self.assertEqual(verif["a@x.cl"], "✓ en directorio: Ana")
        self.assertIn("NO está en el directorio", verif["fake@contoso.cl"])


if __name__ == "__main__":
    unittest.main()
