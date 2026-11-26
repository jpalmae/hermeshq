import tempfile
import unittest
from pathlib import Path

from hermeshq.services.hermes_runtime import HermesRuntime


class TestArchivosPreviosAislamiento(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "sharepoint_downloads").mkdir()
        (self.ws / "sharepoint_downloads" / "confidencial.pdf").write_bytes(b"x")
        self.hist = [{"role": "user", "content": "revisa sharepoint_downloads/confidencial.pdf"}]

    def test_una_conversacion_no_ve_archivos_que_no_menciona(self):
        meta = {"source": "public_chat", "thread_user_id": "u-real"}
        out = HermesRuntime._prompt_with_previous_files("P", self.ws, [{"role": "user", "content": "hola"}], meta)
        self.assertEqual(out, "P")

    def test_tras_reset_sin_historial_no_hay_nota(self):
        self.assertEqual(HermesRuntime._prompt_with_previous_files("P", self.ws, [], None), "P")

    def test_authenticated_public_session_sees_the_note(self):
        meta = {"source": "public_chat", "thread_user_id": "u-real"}
        out = HermesRuntime._prompt_with_previous_files("P", self.ws, self.hist, meta)
        self.assertIn("confidencial.pdf", out)

    def test_anonymous_public_session_does_not_see_the_note(self):
        meta = {"source": "public_chat", "thread_user_id": "__public_chat__"}
        out = HermesRuntime._prompt_with_previous_files("P", self.ws, self.hist, meta)
        self.assertEqual(out, "P")
        self.assertNotIn("confidencial.pdf", out)

    def test_no_metadata_keeps_the_note(self):
        out = HermesRuntime._prompt_with_previous_files("P", self.ws, self.hist, None)
        self.assertIn("confidencial.pdf", out)

    def test_sin_dueno_definido_only_for_anonymous_public(self):
        self.assertTrue(
            HermesRuntime._sin_dueno_definido({"source": "public_chat", "thread_user_id": "__public_chat__"})
        )
        self.assertTrue(HermesRuntime._sin_dueno_definido({"source": "public_chat"}))
        self.assertFalse(HermesRuntime._sin_dueno_definido({"source": "public_chat", "thread_user_id": "u1"}))
        self.assertFalse(HermesRuntime._sin_dueno_definido({"source": "teams"}))
        self.assertFalse(HermesRuntime._sin_dueno_definido(None))

    def test_auto_reset_note_omits_files_for_anonymous(self):
        base = {"auto_reset_aviso": {"fallos": 3, "minutos": 40}, "source": "public_chat"}
        anon = HermesRuntime._nota_reinicio_automatico({**base, "thread_user_id": "__public_chat__"}, self.ws)
        self.assertNotIn("confidencial.pdf", anon)
        conocido = HermesRuntime._nota_reinicio_automatico({**base, "thread_user_id": "u1"}, self.ws)
        self.assertNotIn("confidencial.pdf", conocido)
        self.assertIn("contexto limpio", conocido)


if __name__ == "__main__":
    unittest.main()
