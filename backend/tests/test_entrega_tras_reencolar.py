import unittest

from hermeshq.services.execution_lease import entrega_tras_reencolar


class TestEntregaTrasReencolar(unittest.TestCase):
    def test_chat_publico_reencolado_se_entrega_por_notificacion(self):
        marcado = entrega_tras_reencolar({"source": "public_chat", "thread_user_id": "u1"})
        self.assertEqual(marcado, {"source": "public_chat", "thread_user_id": "u1", "respuesta_tardia": True})

    def test_crons_segundo_plano_y_tareas_web_no_cambian(self):
        for metadata in (
            {"source": "public_chat", "scheduled": True},
            {"source": "public_chat", "background_job": True},
            {"source": "agent_conversation"},
            {},
        ):
            self.assertNotIn("respuesta_tardia", entrega_tras_reencolar(metadata))

    def test_no_muta_el_original(self):
        original = {"source": "public_chat"}
        entrega_tras_reencolar(original)
        self.assertEqual(original, {"source": "public_chat"})


if __name__ == "__main__":
    unittest.main()
