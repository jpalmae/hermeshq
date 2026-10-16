import importlib.util
import unittest
from pathlib import Path

from hermeshq.routers.public_chat import _friendly_task_error

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_under_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class InternalWarningsTests(unittest.TestCase):
    def test_engine_warnings_are_removed_but_the_narration_is_kept(self):
        bruto = (
            "⚠️  Response truncated (finish_reason='length') - model hit max output tokens"
            "⚠️  Truncated tool call detected — retrying API call...Primera parte generada."
        )
        self.assertEqual(runner.limpiar_aviso_interno(bruto), "Primera parte generada.")
        self.assertEqual(runner.limpiar_aviso_interno("❌ Max retries (3) for invalid tool calls exceeded."), "")
        self.assertEqual(
            runner.limpiar_aviso_interno("Hola [Tool loop warning: same_tool_failure_warning; count=3] listo"),
            "Hola  listo",
        )
        self.assertEqual(
            runner.limpiar_aviso_interno("Texto normal con ⚠️ una alerta propia."),
            "Texto normal con ⚠️ una alerta propia.",
        )

    def test_a_missing_final_response_never_breaks_on_none_chunks(self):
        self.assertEqual(runner.texto_final({"final_response": None}, [None, "Hola ", None, "mundo"]), "Hola mundo")
        self.assertEqual(runner.texto_final({"final_response": " Listo "}, ["x"]), "Listo")
        self.assertEqual(runner.texto_final({}, []), "")


class FriendlyErrorTests(unittest.TestCase):
    def test_invalid_tool_call_gets_a_clear_message(self):
        msg = _friendly_task_error({"error": "Hermes runtime stopped early: Model generated invalid tool call: "})
        self.assertIn("llamada inválida", msg)
        self.assertIn("por partes", msg)


class RecoverFromStreamTests(unittest.TestCase):
    def test_recovers_the_last_message_when_the_process_ended_cleanly(self):
        from hermeshq.services.hermes_runtime import HermesRuntime

        segmentos = ["Leo los documentos…", "Listo ✅ El informe quedó creado y guardado en tu carpeta.", ""]
        recuperado = HermesRuntime._recover_result_from_stream(segmentos, 0)
        self.assertEqual(recuperado["final_response"], "Listo ✅ El informe quedó creado y guardado en tu carpeta.")
        self.assertTrue(recuperado["recovered"])

    def test_nothing_is_recovered_after_a_crash_or_without_text(self):
        from hermeshq.services.hermes_runtime import HermesRuntime

        self.assertIsNone(HermesRuntime._recover_result_from_stream(["Listo, todo bien con el informe."], 1))
        self.assertIsNone(HermesRuntime._recover_result_from_stream(["", " "], 0))
        self.assertIsNone(HermesRuntime._recover_result_from_stream(["ok"], 0))


class StdoutSwapTests(unittest.TestCase):
    def test_events_still_reach_the_original_channel_if_the_agent_replaces_stdout(self):
        import io
        import os
        import sys
        import tempfile

        with tempfile.TemporaryFile("w+b") as destino:
            copia_fd1 = os.dup(1)
            os.dup2(destino.fileno(), 1)
            try:
                runner._abrir_salida()
                original = sys.stdout
                sys.stdout = io.StringIO()
                try:
                    runner._emit({"event": "result", "final_response": "Listo ✅"})
                finally:
                    sys.stdout = original
                runner._SALIDA.flush()
            finally:
                os.dup2(copia_fd1, 1)
                os.close(copia_fd1)
                runner._SALIDA.close()
                runner._SALIDA = None
            destino.seek(0)
            contenido = destino.read().decode("utf-8")
        self.assertIn('"event": "result"', contenido)
        self.assertIn("Listo ✅", contenido)


class PreviousFilesNoteTests(unittest.TestCase):
    def test_recent_downloads_and_data_files_are_listed_and_old_ones_are_not(self):
        import os
        import tempfile
        import time
        from pathlib import Path

        from hermeshq.services.hermes_runtime import HermesRuntime

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "sharepoint_downloads").mkdir()
            (raiz / "work" / "licencias").mkdir(parents=True)
            reciente = raiz / "sharepoint_downloads" / "aaa_Licencias.xlsx"
            reciente.write_bytes(b"x")
            datos = raiz / "work" / "licencias" / "datos.md"
            datos.write_text("datos", encoding="utf-8")
            viejo = raiz / "sharepoint_downloads" / "viejo.pdf"
            viejo.write_bytes(b"x")
            hace_un_dia = time.time() - 86400
            os.utime(viejo, (hace_un_dia, hace_un_dia))
            nota = HermesRuntime._previous_files_note(raiz)
            self.assertIn("sharepoint_downloads/aaa_Licencias.xlsx", nota)
            self.assertIn("work/licencias/datos.md", nota)
            self.assertNotIn("viejo.pdf", nota)
            self.assertIn("NO los vuelvas a buscar", nota)

    def test_the_note_is_only_added_when_there_is_conversation_history(self):
        import tempfile
        from pathlib import Path

        from hermeshq.services.hermes_runtime import HermesRuntime

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "sharepoint_downloads").mkdir()
            (raiz / "sharepoint_downloads" / "a.xlsx").write_bytes(b"x")
            self.assertEqual(HermesRuntime._prompt_with_previous_files("hola", raiz, []), "hola")
            self.assertEqual(HermesRuntime._prompt_with_previous_files("hola", raiz, None), "hola")
            con = HermesRuntime._prompt_with_previous_files("hola", raiz, [{"role": "user", "content": "x"}])
            self.assertTrue(con.startswith("hola\n\n[Archivos de turnos anteriores"))
