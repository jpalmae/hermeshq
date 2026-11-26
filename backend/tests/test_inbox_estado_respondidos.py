import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_inbox_estado import AHORA, _correo, plugin


class TestRespondidosSalenDePendientes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.a = "A" * 80
        self.b = "B" * 80

    def _llamar(self, inbox, enviados, **args):
        def fake_graph(method, path, token, *a, **k):
            if "sentitems" in path:
                return {"value": [{"conversationId": c, "sentDateTime": f} for c, f in enviados]}
            return {"value": inbox}

        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", side_effect=fake_graph),
        ):
            return json.loads(plugin._inbox_estado_tool({"ahora": AHORA, **args}))

    def test_a_replied_but_still_unread_email_leaves_pending(self):
        inbox_pendiente = [
            {"id": self.a, "conversationId": "C1", "receivedDateTime": "2026-10-15T10:00:00Z"},
            {"id": self.b, "conversationId": "C2", "receivedDateTime": "2026-10-15T10:10:00Z"},
        ]
        self._llamar(
            inbox_pendiente,
            [],
            nuevos=[
                _correo(self.a, "CALIENTE", "2026-10-15T10:00:00Z"),
                _correo(self.b, "CALIENTE", "2026-10-15T10:10:00Z"),
            ],
        )
        out = self._llamar(inbox_pendiente, [("C1", "2026-10-15T11:00:00Z")])
        asuntos = [c["asunto"] for c in out["calientes"]]
        self.assertEqual(asuntos, [f"Asunto {self.b}"])
        self.assertEqual(out["totales"]["pendientes"], 1)

    def test_new_follow_up_in_already_answered_thread_stays_pending(self):
        inbox_pendiente = [
            {"id": self.a, "conversationId": "C1", "receivedDateTime": "2026-10-26T09:00:00Z"},
        ]
        self._llamar(
            inbox_pendiente,
            [],
            nuevos=[_correo(self.a, "CALIENTE", "2026-10-26T09:00:00Z")],
        )
        out = self._llamar(inbox_pendiente, [("C1", "2026-10-20T11:00:00Z")])
        asuntos = [c["asunto"] for c in out["calientes"]]
        self.assertEqual(asuntos, [f"Asunto {self.a}"])
        self.assertEqual(out["totales"]["pendientes"], 1)


if __name__ == "__main__":
    unittest.main()
