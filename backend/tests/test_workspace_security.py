from pathlib import Path

import pytest

from hermeshq.services.workspace_manager import WorkspaceManager


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[WorkspaceManager, str, Path]:
    manager = WorkspaceManager(tmp_path / "workspaces")
    agent_id = "agent-1"
    root = Path(manager.create_workspace(agent_id, "Agent", None, None))
    (root / ".hermes" / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (root / ".hermes" / "auth.json").write_text('{"access_token":"secret"}', encoding="utf-8")
    (root / "work" / "notes.txt").write_text("safe", encoding="utf-8")
    return manager, agent_id, root


def test_workspace_listing_is_rooted_in_editable_work_directory(workspace) -> None:
    manager, agent_id, _root = workspace
    entries = manager.list_workspace_files(agent_id)
    assert [entry["path"] for entry in entries] == ["notes.txt"]
    assert manager.get_workspace_size(agent_id) == 4


def test_workspace_api_cannot_read_runtime_credentials(workspace) -> None:
    manager, agent_id, _root = workspace
    with pytest.raises(ValueError, match="escapes workspace"):
        manager.read_workspace_file(agent_id, "../.hermes/.env")
    with pytest.raises(FileNotFoundError):
        manager.read_workspace_file(agent_id, ".hermes/auth.json")


def test_workspace_api_cannot_write_runtime_configuration(workspace) -> None:
    manager, agent_id, root = workspace
    with pytest.raises(ValueError, match="escapes workspace"):
        manager.write_workspace_file(agent_id, "../.hermes/config.yaml", "malicious")
    assert not (root / ".hermes" / "config.yaml").exists()


def test_workspace_symlink_cannot_escape_editable_directory(workspace) -> None:
    manager, agent_id, root = workspace
    (root / "work" / "runtime-link").symlink_to(root / ".hermes", target_is_directory=True)
    with pytest.raises(ValueError, match="escapes workspace"):
        manager.read_workspace_file(agent_id, "runtime-link/.env")
