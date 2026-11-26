import unittest
from types import SimpleNamespace

from hermeshq.services.agent_supervisor import clave_sesion_runtime
from hermeshq.services.public_chat_service import PublicChatService


class EpocaDeSesionRuntime(unittest.TestCase):
    def test_sin_epoca_o_epoca_cero_la_clave_no_cambia(self):
        self.assertEqual(clave_sesion_runtime("s1", None), "s1")
        self.assertEqual(clave_sesion_runtime("s1", 0), "s1")

    def test_cada_reinicio_usa_una_clave_de_sesion_distinta(self):
        self.assertEqual(clave_sesion_runtime("s1", 1), "s1#e1")
        self.assertEqual(clave_sesion_runtime("s1", 2), "s1#e2")
        self.assertNotEqual(clave_sesion_runtime("s1", 1), clave_sesion_runtime("s1", 2))


class MarcaDeReinicioLlevaLaEpoca(unittest.TestCase):
    def test_la_marca_de_reset_registra_la_nueva_epoca(self):
        sesion = SimpleNamespace(agent_id="agente-1")
        marca = PublicChatService._reset_marker(sesion, "s1", "u1", epoca=3)
        self.assertEqual(marca.metadata_json["conversation_epoch"], 3)
        self.assertTrue(marca.metadata_json["reset"])
        self.assertEqual(marca.metadata_json["public_session_id"], "s1")


if __name__ == "__main__":
    unittest.main()
