"""Tests for PiRuntime model resolution and task lifecycle."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermeshq.services.runtime_base import RuntimeBase, RuntimeExecutionResult, RuntimeExecutionError
from hermeshq.services.pi_rpc_client import PiRpcClient


class TestRuntimeExecutionResult:
    def test_fields(self):
        r = RuntimeExecutionResult(
            final_response="hello",
            messages=[{"role": "assistant", "content": "hello"}],
            tool_calls=[],
            tokens_used=10,
            iterations=1,
            engine="pi",
            response_attachments=[],
        )
        assert r.final_response == "hello"
        assert r.engine == "pi"


class TestRuntimeBase:
    def test_hermes_implements_base(self):
        from hermeshq.services.hermes_runtime import HermesRuntime

        assert issubclass(HermesRuntime, RuntimeBase)

    def test_pi_implements_base(self):
        from hermeshq.services.pi_runtime import PiRuntime

        assert issubclass(PiRuntime, RuntimeBase)


class TestPiInstallationWritesModels:
    def test_write_models_creates_valid_json(self, tmp_path):
        from hermeshq.services.pi_installation import PiInstallationManager

        vault = MagicMock()
        wm = MagicMock()
        agent = MagicMock(
            model="deepseek-ai/deepseek-v4-flash-0731",
            provider="nvidia-nim",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_ref="NIM_KEY",
        )
        agent.pi_config = None
        agent.permission_policy_id = None

        manager = PiInstallationManager(vault, wm)
        pi_home = tmp_path / "agent" / ".pi"
        pi_home.mkdir(parents=True)

        manager._write_models(agent, pi_home)

        data = json.loads((pi_home / "models.json").read_text())
        # nvidia-nim maps to 'nvidia' provider key
        assert "nvidia" in data["providers"]
        ids = [m["id"] for m in data["providers"]["nvidia"]["models"]]
        assert "deepseek-ai/deepseek-v4-flash-0731" in ids

    def test_write_settings_contains_compaction(self, tmp_path):
        from hermeshq.services.pi_installation import PiInstallationManager

        vault = MagicMock()
        wm = MagicMock()
        agent = MagicMock(pi_config={"project_trust": "always"})

        manager = PiInstallationManager(vault, wm)
        pi_home = tmp_path / ".pi"
        pi_home.mkdir(parents=True)

        manager._write_settings(agent, pi_home)

        data = json.loads((pi_home / "settings.json").read_text())
        assert "compaction" in data
