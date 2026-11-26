import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def _graph(self, method, path, token, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        return {}


class TestSendBodyAtado(_Base):
    def _run(self, args1, args2):
        def correr(a, actual):
            mensaje = "sí" if a.get("aprobado_por_el_usuario") else "prepáralo"
            carga = json.dumps({"task_id": actual, "metadata": {}, "mensaje": mensaje})
            with (
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
                patch.object(self.mod, "_current_task_id", return_value=actual),
            ):
                return json.loads(self.mod._send_email_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            first = correr(args1, "tarea-1")
            self.calls.clear()
            second = correr({**args2, "aprobado_por_el_usuario": True}, "tarea-2")
            return first, second

    def test_mismo_cuerpo_aprobado_se_envia(self):
        base = {"to": "diego@socio.cl", "subject": "Urgente", "body": "Hola, por favor revisa."}
        first, second = self._run(base, base)
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second["success"], second)
        self.assertIn(("POST", "/me/sendMail"), [(m, p) for m, p, _ in self.calls])

    def test_cuerpo_reformulado_pide_otra_vista_previa(self):
        base = {"to": "diego@socio.cl", "subject": "Urgente", "body": "Hola, por favor revisa."}
        first, second = self._run(base, {**base, "body": "Estimado Daniel, te pido revisar esto. Gracias."})
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertFalse(second["success"])
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertNotIn("/me/sendMail", [p for _, p, _ in self.calls])

    def test_changed_recipient_retriggers_gate(self):
        base = {"to": "diego@socio.cl", "subject": "Urgente", "body": "Hola"}
        first, second = self._run(base, {**base, "to": "otro@x.cl"})
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertNotIn("/me/sendMail", [p for _, p, _ in self.calls])

    def test_changed_subject_retriggers_gate(self):
        base = {"to": "diego@socio.cl", "subject": "Urgente", "body": "Hola"}
        first, second = self._run(base, {**base, "subject": "Otra cosa"})
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertNotIn("/me/sendMail", [p for _, p, _ in self.calls])


class TestForwardCommentAtado(_Base):
    def _run(self, args1, args2):
        msg = {
            "subject": "Alerta CVE",
            "from": {"emailAddress": {"address": "org@x.cl"}},
            "receivedDateTime": "2026-10-22T00:00:00Z",
        }

        def correr(a, actual):
            mensaje = "sí" if a.get("aprobado_por_el_usuario") else "prepáralo"
            carga = json.dumps({"task_id": actual, "metadata": {}, "mensaje": mensaje})
            with (
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
                patch.object(self.mod, "_current_task_id", return_value=actual),
            ):
                return json.loads(self.mod._forward_email_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolve_message_id", side_effect=lambda x: x),
            patch.object(self.mod, "_message_for_action", return_value=(msg, None)),
            patch.object(self.mod, "_list_attachments", return_value=([], None)),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            first = correr(args1, "tarea-1")
            self.calls.clear()
            second = correr({**args2, "aprobado_por_el_usuario": True}, "tarea-2")
            return first, second

    def test_mismo_comentario_aprobado_se_reenvia(self):
        args = {"message_id": "MSG18", "to": "diego@socio.cl", "comment": "Hola Daniel, reenvío la alerta."}
        first, second = self._run(args, args)
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second["success"], second)
        self.assertTrue(any(p.endswith("/createForward") for _, p, _ in self.calls))
        self.assertTrue(any(p.endswith("/send") for _, p, _ in self.calls))

    def test_comentario_reescrito_pide_otra_vista_previa(self):
        first, second = self._run(
            {"message_id": "MSG18", "to": "diego@socio.cl", "comment": "Hola Daniel, reenvío la alerta."},
            {"message_id": "MSG18", "to": "diego@socio.cl", "comment": "Estimado Daniel: te reenvío esto. Urgente."},
        )
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertFalse(any(p.endswith(("/forward", "/createForward", "/send")) for _, p, _ in self.calls))

    def test_changed_recipient_retriggers_gate(self):
        first, second = self._run(
            {"message_id": "MSG18", "to": "diego@socio.cl", "comment": "x"},
            {"message_id": "MSG18", "to": "intruso@x.cl", "comment": "x"},
        )
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertFalse(any(p.endswith(("/forward", "/createForward", "/send")) for _, p, _ in self.calls))


class TestReplyCommentAtado(_Base):
    def _run(self, args1, args2):
        msg = {"subject": "Re: algo", "from": {"emailAddress": {"address": "org@x.cl"}}, "toRecipients": []}

        def correr(a, actual):
            mensaje = "sí" if a.get("aprobado_por_el_usuario") else "prepáralo"
            carga = json.dumps({"task_id": actual, "metadata": {}, "mensaje": mensaje})
            with (
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
                patch.object(self.mod, "_current_task_id", return_value=actual),
            ):
                return json.loads(self.mod._reply_email_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_resolve_message_id", side_effect=lambda x: x),
            patch.object(self.mod, "_message_for_action", return_value=(msg, None)),
            patch.object(self.mod, "_graph", side_effect=self._graph),
        ):
            first = correr(args1, "tarea-1")
            self.calls.clear()
            second = correr({**args2, "aprobado_por_el_usuario": True}, "tarea-2")
            return first, second

    def test_misma_respuesta_aprobada_se_envia(self):
        args = {"message_id": "MSG1", "comment": "Gracias, lo veo."}
        first, second = self._run(args, args)
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second["success"], second)
        self.assertTrue(any(p.endswith("/reply") for _, p, _ in self.calls))

    def test_respuesta_reescrita_pide_otra_vista_previa(self):
        first, second = self._run(
            {"message_id": "MSG1", "comment": "Gracias, lo veo."},
            {"message_id": "MSG1", "comment": "Muchas gracias, lo reviso hoy mismo."},
        )
        self.assertTrue(first.get("requiere_aprobacion"))
        self.assertTrue(second.get("requiere_aprobacion"))
        self.assertFalse(any(p.endswith("/reply") for _, p, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
