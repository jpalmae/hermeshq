import json
from types import SimpleNamespace

import pytest

from hermeshq.services.pi_installation import PiInstallationManager
from hermeshq.services.workspace_manager import WorkspaceManager


@pytest.mark.asyncio
async def test_pi_configuration_is_managed_outside_the_agent_workspace(tmp_path) -> None:
    workspace_manager = WorkspaceManager(tmp_path)
    agent_id = "11111111-1111-4111-8111-111111111111"
    workspace = workspace_manager.build_workspace_path(agent_id)
    legacy_extension = workspace / ".pi" / "extensions" / "hermeshq-security.ts"
    legacy_extension.parent.mkdir(parents=True)
    legacy_extension.write_text("stale", encoding="utf-8")
    agent = SimpleNamespace(
        id=agent_id,
        api_key_ref=None,
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        provider="openai",
        pi_config={"project_trust": "always"},
        integration_configs={"gmail": {}},
    )
    manager = PiInstallationManager(SimpleNamespace(), workspace_manager)

    await manager.sync_agent_installation(agent)

    pi_home = workspace_manager.build_pi_config_path(agent_id)
    assert pi_home == tmp_path / "_runtime_config" / "pi" / f"agent-{agent_id}"
    assert workspace not in pi_home.parents
    assert json.loads((pi_home / "settings.json").read_text())["defaultProjectTrust"] == "never"
    security_extension = (pi_home / "extensions" / "hermeshq-security.ts").read_text()
    assert "/control/permissions/evaluate" in security_extension
    assert "ALLOWED_TOOLS" not in security_extension
    integration_extension = (pi_home / "extensions" / "hermeshq-integrations.ts").read_text()
    assert "/control/agents/" in integration_extension
    assert "/internal/control/agents/" not in integration_extension
    assert legacy_extension.exists() is False


def test_workspace_deletion_also_deletes_managed_pi_configuration(tmp_path) -> None:
    workspace_manager = WorkspaceManager(tmp_path)
    agent_id = "11111111-1111-4111-8111-111111111111"
    workspace_manager.build_workspace_path(agent_id).mkdir()
    workspace_manager.build_pi_config_path(agent_id).mkdir(parents=True)

    workspace_manager.delete_workspace(agent_id)

    assert workspace_manager.build_workspace_path(agent_id).exists() is False
    assert workspace_manager.build_pi_config_path(agent_id).exists() is False
