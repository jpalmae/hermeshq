import unittest

from hermeshq.models.task import Task
from hermeshq.services import hermes_runtime as hr
from hermeshq.services.agent_supervisor import apply_failure_telemetry, apply_usage_metadata


class ClassifyEndReasonTests(unittest.TestCase):
    def test_messages_map_to_the_closed_vocabulary(self):
        self.assertEqual(
            hr._classify_end_reason("Hermes runtime produced no output for 600s and was stopped"), "idle_timeout"
        )
        self.assertEqual(hr._classify_end_reason("Hermes runtime exceeded the task timeout of 240s"), "timeout")
        self.assertEqual(hr._classify_end_reason("Model generated invalid tool call: foo"), "invalid_tool_calls")
        self.assertEqual(hr._classify_end_reason("Hermes runtime stopped early: partial result"), "stopped_early")
        self.assertEqual(hr._classify_end_reason("boom"), "runtime_error")

    def test_every_hint_is_in_the_vocabulary(self):
        for reason in ("idle_timeout", "timeout", "invalid_tool_calls", "stopped_early", "runtime_error"):
            self.assertIn(reason, hr.END_REASON_VOCAB)


class SafeTraceTailTests(unittest.TestCase):
    def test_api_key_is_redacted_and_tail_is_bounded(self):
        key = "sk-supersecret-value-123"
        texto = "x" * 5000 + f" authorization uses {key} and failed"
        tail = hr._safe_trace_tail(texto, key)
        self.assertNotIn(key, tail)
        self.assertLessEqual(len(tail), 2000)

    def test_none_key_leaves_the_text_alone(self):
        self.assertEqual(hr._safe_trace_tail("hola", None), "hola")


class AccumulateTraceEventTests(unittest.TestCase):
    def test_tool_and_step_events_fold_into_the_trace(self):
        traza = {"eventos": {}}
        hr.HermesRuntime._accumulate_trace_event(
            traza, {"event": "tool", "name": "sharepoint_search", "ok": False, "ms": 70}
        )
        hr.HermesRuntime._accumulate_trace_event(
            traza, {"event": "tool", "name": "hq_leer_varios", "ok": True, "ms": 1200}
        )
        hr.HermesRuntime._accumulate_trace_event(traza, {"event": "step", "n": 4})
        hr.HermesRuntime._accumulate_trace_event(traza, {"event": "step", "n": 2})
        self.assertEqual([t["status"] for t in traza["herramientas"]], ["error", "completed"])
        self.assertEqual(traza["herramientas"][0]["duration_ms"], 70)
        self.assertEqual(traza["pasos"], 4)


class BuildPartialTests(unittest.TestCase):
    def _traza(self):
        return {
            "eventos": {"tool": 2, "step": 3},
            "lineas": 10,
            "pasos": 25,
            "herramientas": [
                {"name": "hq_leer_varios", "status": "completed", "duration_ms": 1200},
                {"name": "sharepoint_search", "status": "error", "duration_ms": 50},
            ],
            "error_evento": {
                "error_type": "RuntimeError",
                "traceback": "Traceback (most recent call last): boom",
                "usage": {"total_tokens": 42, "reasoning_tokens": 10},
            },
        }

    def test_partial_keeps_real_tool_status_and_iterations(self):
        partial = hr._build_partial(self._traza(), "timeout", "stderr", api_key=None)
        self.assertEqual(partial["end_reason"], "timeout")
        self.assertEqual(partial["iterations"], 25)
        self.assertEqual([t["status"] for t in partial["tool_calls"]], ["completed", "error"])
        self.assertEqual(partial["error_type"], "RuntimeError")
        self.assertIn("boom", partial["error_trace"])
        self.assertEqual(partial["run"]["tools"], 2)
        self.assertEqual(partial["run"]["usage"], {"total_tokens": 42, "reasoning_tokens": 10})

    def test_token_counts_survive_and_are_not_mistaken_for_secrets(self):
        partial = hr._build_partial(self._traza(), "timeout", "", api_key=None)
        self.assertEqual(partial["run"]["usage"]["total_tokens"], 42)

    def test_stderr_tail_is_used_when_there_is_no_runner_traceback(self):
        partial = hr._build_partial({"eventos": {}, "pasos": 0}, "idle_timeout", "stderr says X", api_key=None)
        self.assertIn("stderr says X", partial["error_trace"])
        self.assertEqual(partial["tool_calls"], [])
        self.assertEqual(partial["iterations"], 0)


class AttachPartialTests(unittest.TestCase):
    def test_hq_partial_is_attached_to_the_exception(self):
        exc = hr.RuntimeExecutionError("boom")
        hr._attach_partial(exc, {"pasos": 3, "eventos": {}}, "runtime_error", "", None)
        self.assertEqual(exc.hq_partial["iterations"], 3)
        self.assertEqual(exc.hq_partial["end_reason"], "runtime_error")


class UsageMetadataTests(unittest.TestCase):
    def test_usage_is_merged_without_clobbering_other_keys(self):
        task = Task(prompt="p", metadata_json={"source": "public_chat"})
        apply_usage_metadata(task, {"total_tokens": 2000, "api_calls": 7})
        self.assertEqual(task.metadata_json["usage"], {"total_tokens": 2000, "api_calls": 7})
        self.assertEqual(task.metadata_json["source"], "public_chat")

    def test_empty_usage_is_a_noop(self):
        task = Task(prompt="p", metadata_json={"source": "x"})
        apply_usage_metadata(task, None)
        self.assertNotIn("usage", task.metadata_json)


class FailureTelemetryTests(unittest.TestCase):
    def test_partial_tool_calls_and_end_reason_are_persisted_with_real_status(self):
        task = Task(prompt="p", metadata_json={"source": "public_chat"}, tool_calls=[], iterations=0)
        exc = hr.RuntimeExecutionError("timeout")
        exc.hq_partial = {
            "end_reason": "timeout",
            "tool_calls": [{"name": "x", "status": "error", "duration_ms": 10}],
            "iterations": 25,
            "error_type": "RuntimeError",
            "error_trace": "tail",
            "run": {"tools": 1, "steps": 25, "usage": {"total_tokens": 7}},
        }
        apply_failure_telemetry(task, exc)
        self.assertEqual(task.tool_calls[0]["status"], "error")
        self.assertEqual(task.iterations, 25)
        self.assertEqual(task.metadata_json["end_reason"], "timeout")
        self.assertEqual(task.metadata_json["error_type"], "RuntimeError")
        self.assertEqual(task.metadata_json["error_trace"], "tail")
        self.assertEqual(task.metadata_json["usage"], {"total_tokens": 7})

    def test_without_partial_it_falls_back_to_the_default_reason(self):
        task = Task(prompt="p", metadata_json={})
        apply_failure_telemetry(task, ValueError("x"), default_reason="cancelled")
        self.assertEqual(task.metadata_json["end_reason"], "cancelled")
        self.assertEqual(task.metadata_json["error_type"], "ValueError")

    def test_a_failed_run_is_no_longer_forced_to_an_empty_completed_shape(self):
        task = Task(prompt="p", metadata_json={}, tool_calls=[], iterations=0)
        exc = hr.RuntimeExecutionError("idle")
        exc.hq_partial = {
            "end_reason": "idle_timeout",
            "tool_calls": [{"name": "t", "status": "completed"}],
            "iterations": 12,
        }
        apply_failure_telemetry(task, exc)
        self.assertNotEqual(task.tool_calls, [])
        self.assertEqual(task.iterations, 12)
        self.assertEqual(task.metadata_json["end_reason"], "idle_timeout")


if __name__ == "__main__":
    unittest.main()
