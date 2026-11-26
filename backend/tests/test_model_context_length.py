import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from hermeshq.config import Settings
from hermeshq.services.hermes_installation.manager import HermesInstallationManager


def _write_config(context_length, memory_limit=3000, user_limit=1800, threshold=0.75, target_ratio=0.50):
    manager = HermesInstallationManager.__new__(HermesInstallationManager)
    agent = SimpleNamespace(
        runtime_profile="standard",
        model="qwen3.8-27b",
        max_iterations=30,
        auxiliary_models=None,
        enabled_toolsets=[],
        base_url="",
    )
    selection = SimpleNamespace(source="bundled", detected_version="0.13.0", effective_version="0.13.0", release_tag="")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with (
            patch.object(manager, "_model_provider_for_agent", return_value="custom"),
            patch.object(manager, "_effective_provider_base_url", return_value="https://llm.example/v1"),
            patch.object(manager, "_uses_custom_openai_provider", return_value=False),
            patch.object(manager, "_voice_runtime_overrides", return_value={}),
            patch.object(manager, "_interaction_runtime_overrides", return_value={}),
            patch(
                "hermeshq.services.hermes_installation.manager.get_settings",
                return_value=SimpleNamespace(
                    model_context_length=context_length,
                    agent_memory_char_limit=memory_limit,
                    user_memory_char_limit=user_limit,
                    compression_threshold=threshold,
                    compression_target_ratio=target_ratio,
                ),
            ),
        ):
            manager._write_config(
                agent, home, "prompt", [], None, selection, effective_base_url="https://llm.example/v1"
            )
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


class ToolLoopGuardrailsConfigTests(unittest.TestCase):
    def test_hard_stop_is_enabled_so_repeated_failing_calls_are_blocked(self):
        guardrails = _write_config(0)["tool_loop_guardrails"]
        self.assertTrue(guardrails["warnings_enabled"])
        self.assertTrue(guardrails["hard_stop_enabled"])
        self.assertEqual(guardrails["hard_stop_after"]["exact_failure"], 5)
        self.assertEqual(guardrails["hard_stop_after"]["same_tool_failure"], 8)


class ModelContextLengthTests(unittest.TestCase):
    def test_configured_value_is_written_to_model_section(self):
        config = _write_config(131072)
        self.assertEqual(config["model"]["context_length"], 131072)
        self.assertEqual(config["model"]["default"], "qwen3.8-27b")
        self.assertEqual(config["model"]["base_url"], "https://llm.example/v1")

    def test_disabled_by_default_leaves_detection_to_hermes(self):
        self.assertEqual(Settings(debug=True, jwt_secret="x" * 40).model_context_length, 0)
        config = _write_config(0)
        self.assertNotIn("context_length", config["model"])

    def test_negative_value_is_ignored(self):
        config = _write_config(-5)
        self.assertNotIn("context_length", config["model"])


class CompressionTests(unittest.TestCase):
    def test_compression_section_is_written_with_a_reachable_target(self):
        comp = _write_config(0)["compression"]
        self.assertEqual(comp["enabled"], True)
        self.assertEqual(comp["threshold"], 0.75)
        self.assertEqual(comp["target_ratio"], 0.50)
        self.assertEqual(comp["protect_last_n"], 20)
        self.assertLess(comp["target_ratio"], comp["threshold"])

    def test_settings_defaults_are_the_tuned_values(self):
        settings = Settings(debug=True, jwt_secret="x" * 40)
        self.assertEqual(settings.compression_threshold, 0.75)
        self.assertEqual(settings.compression_target_ratio, 0.50)

    def test_values_come_from_settings(self):
        comp = _write_config(0, threshold=0.8, target_ratio=0.4)["compression"]
        self.assertEqual(comp["threshold"], 0.8)
        self.assertEqual(comp["target_ratio"], 0.4)


class MemoryLimitTests(unittest.TestCase):
    def test_defaults_are_a_little_above_the_hermes_ones(self):
        settings = Settings(debug=True, jwt_secret="x" * 40)
        self.assertEqual(settings.agent_memory_char_limit, 3000)
        self.assertEqual(settings.user_memory_char_limit, 1800)

    def test_the_memory_section_keeps_memory_enabled_and_carries_the_limits(self):
        memory = _write_config(0)["memory"]
        self.assertEqual(
            memory,
            {
                "memory_enabled": True,
                "user_profile_enabled": True,
                "memory_char_limit": 3000,
                "user_char_limit": 1800,
            },
        )
        self.assertEqual(_write_config(0, 4000, 2500)["memory"]["memory_char_limit"], 4000)


if __name__ == "__main__":
    unittest.main()
