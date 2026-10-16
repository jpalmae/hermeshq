import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from hermeshq.services.hermes_installation.manager import HermesInstallationManager

EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}


def _config(thinking_mode, auxiliary_models=None):
    manager = HermesInstallationManager.__new__(HermesInstallationManager)
    manager.secret_vault = None
    agent = SimpleNamespace(
        runtime_profile="standard",
        model="qwen3.8-27b",
        max_iterations=30,
        auxiliary_models=auxiliary_models,
        enabled_toolsets=[],
        thinking_mode=thinking_mode,
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
                return_value=SimpleNamespace(model_context_length=0),
            ),
        ):
            manager._write_config(
                agent,
                home,
                "prompt",
                [],
                None,
                selection,
                resolved_aux_api_keys={"__main__": "main-key"},
                effective_model="qwen3.8-27b",
                effective_base_url="https://llm.example/v1",
            )
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


class AuxiliaryThinkingTests(unittest.TestCase):
    def test_off_disables_thinking_for_compression_and_web_extract(self):
        aux = _config("off")["auxiliary"]
        self.assertEqual(aux["compression"]["extra_body"], EXTRA)
        self.assertEqual(aux["web_extract"]["extra_body"], EXTRA)

    def test_vision_is_left_untouched(self):
        self.assertNotIn("extra_body", _config("off")["auxiliary"]["vision"])

    def test_default_and_explicit_on_add_nothing(self):
        for mode in (None, "on"):
            with self.subTest(mode=mode):
                aux = _config(mode)["auxiliary"]
                for task in ("compression", "web_extract", "vision"):
                    self.assertNotIn("extra_body", aux[task])

    def test_manually_configured_task_also_gets_the_setting(self):
        manual = {"compression": {"base_url": "https://aux.example/v1", "model": "qwen3.8-27b"}}
        with patch("hermeshq.services.hermes_installation.manager.resolve_auxiliary_api_key", return_value="aux-key"):
            aux = _config("off", manual)["auxiliary"]
        self.assertEqual(aux["compression"]["base_url"], "https://aux.example/v1")
        self.assertEqual(aux["compression"]["extra_body"], EXTRA)

    def test_each_task_gets_its_own_copy(self):
        aux = _config("off")["auxiliary"]
        aux["compression"]["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
        self.assertFalse(aux["web_extract"]["extra_body"]["chat_template_kwargs"]["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
