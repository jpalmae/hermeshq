import importlib.util
import json
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


class UsageForwardingTests(unittest.TestCase):
    def test_usage_is_forwarded_and_iterations_use_api_calls(self):
        result = {
            "api_calls": 5,
            "input_tokens": 1200,
            "output_tokens": 800,
            "reasoning_tokens": 400,
            "cache_read_tokens": 0,
            "total_tokens": 2000,
            "turn_exit_reason": "stop",
            "messages": [{"role": "assistant", "content": "a"}] * 9,
        }
        payload = runner.build_result_payload(result, "Listo", result["messages"], [])
        self.assertEqual(payload["event"], "result")
        self.assertEqual(payload["iterations"], 5)
        self.assertEqual(payload["usage"]["total_tokens"], 2000)
        self.assertEqual(payload["usage"]["reasoning_tokens"], 400)
        self.assertEqual(payload["usage"]["cache_read_tokens"], 0)
        self.assertNotIn("estimated_cost_usd", payload["usage"])

    def test_tokens_used_stays_a_word_count_for_the_quota(self):
        result = {"api_calls": 3, "total_tokens": 999999}
        payload = runner.build_result_payload(result, "ok", [], [])
        self.assertEqual(payload["tokens_used"], max(256, len(str(result).split())))
        self.assertNotEqual(payload["tokens_used"], 999999)

    def test_iterations_fall_back_to_assistant_messages_without_api_calls(self):
        messages = [{"role": "assistant"}, {"role": "user"}, {"role": "assistant"}]
        payload = runner.build_result_payload({}, "", messages, [])
        self.assertEqual(payload["iterations"], 2)
        self.assertEqual(payload["usage"], {})

    def test_usage_from_result_drops_missing_keys(self):
        self.assertEqual(runner.usage_from_result({"input_tokens": 5, "output_tokens": None}), {"input_tokens": 5})


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
            con = HermesRuntime._prompt_with_previous_files(
                "hola", raiz, [{"role": "user", "content": "usa sharepoint_downloads/a.xlsx"}]
            )
            self.assertTrue(con.startswith("hola\n\n[Archivos de turnos anteriores"))
            sin_mencion = HermesRuntime._prompt_with_previous_files("hola", raiz, [{"role": "user", "content": "x"}])
            self.assertEqual(sin_mencion, "hola")


class RegistroTiemposTests(unittest.TestCase):
    def test_writes_one_line_per_step_event_under_the_workspace(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as cwd:
            registro = runner.RegistroTiempos(cwd, "tarea-1")
            registro.paso_iniciado(1)
            registro.texto_visible()
            registro.texto_visible()
            registro.herramienta("read_file", 120, True)
            registro.paso_iniciado(2)
            registro.fin()
            lineas = (Path(cwd) / ".hermes" / "tiempos" / "tarea-1.jsonl").read_text(encoding="utf-8").splitlines()
            eventos = [json.loads(linea) for linea in lineas]
        self.assertEqual([e["evento"] for e in eventos], ["paso", "primer_texto", "herramienta", "paso", "fin"])
        self.assertEqual(eventos[1]["paso"], 1)
        self.assertEqual(eventos[2]["ms"], 120)
        self.assertTrue(all("t" in e for e in eventos))

    def test_a_workspace_that_cannot_be_written_never_breaks_the_task(self):
        registro = runner.RegistroTiempos("/proc/no-existe-aqui", "tarea-2")
        registro.paso_iniciado(1)
        registro.texto_visible()
        registro.fin()
        self.assertIsNone(registro.ruta)


class RecorteContextoTests(unittest.TestCase):
    def _herramienta(self, id_llamada, nombre, contenido):
        return {"role": "tool", "tool_call_id": id_llamada, "name": nombre, "content": contenido}

    def _llamada(self, id_llamada, nombre):
        return {"role": "assistant", "content": "", "tool_calls": [{"id": id_llamada, "function": {"name": nombre}}]}

    def test_small_histories_are_sent_unchanged(self):
        mensajes = [
            {"role": "user", "content": "hola"},
            self._llamada("a", "read_file"),
            self._herramienta("a", "read_file", "x" * 100),
        ]
        nuevos, recortados = runner.recortar_resultados(mensajes)
        self.assertEqual(recortados, 0)
        self.assertEqual(nuevos, mensajes)

    def test_old_results_are_cut_and_the_last_two_keep_more_room(self):
        grande = "x" * 20000
        corto = "y" * 10000
        mensajes = [
            self._llamada("a", "read_file"),
            self._herramienta("a", "read_file", grande),
            self._llamada("b", "read_file"),
            self._herramienta("b", "read_file", grande),
            self._llamada("c", "read_file"),
            self._herramienta("c", "read_file", corto),
        ]
        nuevos, recortados = runner.recortar_resultados(mensajes)
        self.assertEqual(recortados, 2)
        self.assertIn("caracteres omitidos", nuevos[1]["content"])
        self.assertLess(len(nuevos[1]["content"]), runner.LIMITE_RESULTADO_ANTIGUO + 200)
        self.assertEqual(
            nuevos[3]["content"][: runner.LIMITE_RESULTADO_RECIENTE], "x" * runner.LIMITE_RESULTADO_RECIENTE
        )
        self.assertGreater(len(nuevos[3]["content"]), runner.LIMITE_RESULTADO_ANTIGUO)
        self.assertEqual(nuevos[5]["content"], corto)

    def test_skill_instructions_are_never_cut(self):
        grande = "x" * 20000
        mensajes = [
            self._llamada("s", "skill_view"),
            self._herramienta("s", "skill_view", grande),
            self._llamada("b", "read_file"),
            self._herramienta("b", "read_file", grande),
            self._llamada("c", "read_file"),
            self._herramienta("c", "read_file", grande),
        ]
        nuevos, _ = runner.recortar_resultados(mensajes)
        self.assertEqual(nuevos[1]["content"], grande)

    def test_tool_call_ids_and_the_original_history_are_preserved(self):
        grande = "x" * 20000
        mensajes = [
            self._llamada("a", "read_file"),
            self._herramienta("a", "read_file", grande),
            self._llamada("b", "read_file"),
            self._herramienta("b", "read_file", grande),
            self._llamada("c", "read_file"),
            self._herramienta("c", "read_file", grande),
        ]
        nuevos, _ = runner.recortar_resultados(mensajes)
        self.assertEqual(nuevos[1]["tool_call_id"], "a")
        self.assertEqual(mensajes[1]["content"], grande)

    def test_name_is_resolved_from_the_call_when_the_result_has_none(self):
        grande = "x" * 20000
        mensajes = [
            self._llamada("s", "skill_view"),
            {"role": "tool", "tool_call_id": "s", "content": grande},
            self._llamada("b", "read_file"),
            self._herramienta("b", "read_file", grande),
            self._llamada("c", "read_file"),
            self._herramienta("c", "read_file", grande),
        ]
        nuevos, _ = runner.recortar_resultados(mensajes)
        self.assertEqual(nuevos[1]["content"], grande)


class RegistroRecorteTests(unittest.TestCase):
    def test_each_trim_is_recorded_with_its_step(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as cwd:
            registro = runner.RegistroTiempos(cwd, "tarea-3")
            registro.paso_iniciado(4)
            registro.recorte(2, 17)
            lineas = (Path(cwd) / ".hermes" / "tiempos" / "tarea-3.jsonl").read_text(encoding="utf-8").splitlines()
            evento = json.loads(lineas[-1])
        self.assertEqual(evento["evento"], "recorte")
        self.assertEqual((evento["paso"], evento["recortados"], evento["mensajes"]), (4, 2, 17))


class SkillRepetidaTests(unittest.TestCase):
    def _llamada_skill(self, id_llamada, skill):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": id_llamada, "function": {"name": "skill_view", "arguments": json.dumps({"name": skill})}}
            ],
        }

    def _resultado(self, id_llamada, contenido):
        return {"role": "tool", "tool_call_id": id_llamada, "name": "skill_view", "content": contenido}

    def test_second_load_of_the_same_skill_is_replaced_and_the_first_is_kept(self):
        texto = "instrucciones " * 500
        mensajes = [
            self._llamada_skill("a", "composicion-visual"),
            self._resultado("a", texto),
            self._llamada_skill("b", "composicion-visual"),
            self._resultado("b", texto),
        ]
        nuevos, recortados = runner.recortar_resultados(mensajes)
        self.assertEqual(nuevos[1]["content"], texto)
        self.assertIn("ya se cargó antes en esta tarea", nuevos[3]["content"])
        self.assertIn("composicion-visual", nuevos[3]["content"])
        self.assertEqual(recortados, 1)

    def test_different_skills_are_never_replaced(self):
        mensajes = [
            self._llamada_skill("a", "correo-unificado-bruno"),
            self._resultado("a", "uno"),
            self._llamada_skill("b", "redactar-correos"),
            self._resultado("b", "dos"),
        ]
        nuevos, recortados = runner.recortar_resultados(mensajes)
        self.assertEqual([nuevos[1]["content"], nuevos[3]["content"]], ["uno", "dos"])
        self.assertEqual(recortados, 0)

    def test_the_skill_name_is_read_from_the_arguments(self):
        self.assertEqual(runner._nombre_skill('{"name": "pptx"}'), "pptx")
        self.assertEqual(runner._nombre_skill({"name": "pptx"}), "pptx")
        self.assertEqual(runner._nombre_skill("no es json"), "")
