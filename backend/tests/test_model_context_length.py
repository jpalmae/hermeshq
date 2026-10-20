import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from hermeshq.config import Settings
from hermeshq.services.hermes_installation.manager import HermesInstallationManager


def _write_config(context_length):
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
                return_value=SimpleNamespace(model_context_length=context_length),
            ),
        ):
            manager._write_config(
                agent, home, "prompt", [], None, selection, effective_base_url="https://llm.example/v1"
            )
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


class ModelContextLengthTests(unittest.TestCase):
    def test_disabled_by_default_leaves_detection_to_hermes(self):
        self.assertEqual(Settings(debug=True, jwt_secret="x" * 40).model_context_length, 0)
        config = _write_config(0)
        self.assertNotIn("context_length", config["model"])

    def test_configured_value_is_written_to_model_section(self):
        config = _write_config(131072)
        self.assertEqual(config["model"]["context_length"], 131072)
        self.assertEqual(config["model"]["default"], "qwen3.8-27b")
        self.assertEqual(config["model"]["base_url"], "https://llm.example/v1")

    def test_negative_value_is_ignored(self):
        config = _write_config(-5)
        self.assertNotIn("context_length", config["model"])


if __name__ == "__main__":
    unittest.main()
