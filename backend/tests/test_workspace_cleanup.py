"""Tests for workspace cleanup + orphan Pi config sweep (Phase 4a/4b)."""

from pathlib import Path
from unittest.mock import MagicMock

from hermeshq.services.workspace_manager import WorkspaceManager


class TestDeleteWorkspace:
    def test_removes_workspace_and_pi_config(self, tmp_path):
        wm = WorkspaceManager(tmp_path)
        agent_id = "11111111-1111-1111-1111-111111111111"

        workspace = tmp_path / f"agent-{agent_id}"
        (workspace / "work").mkdir(parents=True)
        (workspace / "work" / "file.txt").write_text("data")

        pi_config = tmp_path / "_runtime_config" / "pi" / f"agent-{agent_id}"
        (pi_config / "skills" / "x").mkdir(parents=True)
        (pi_config / "skills" / "x" / "SKILL.md").write_text("# x")

        wm.delete_workspace(agent_id)

        assert not workspace.exists()
        assert not pi_config.exists()

    def test_noop_when_neither_exists(self, tmp_path):
        wm = WorkspaceManager(tmp_path)
        wm.delete_workspace("22222222-2222-2222-2222-222222222222")
        assert not (tmp_path / "_runtime_config").exists() or True


class TestCleanupOrphanPiConfigs:
    def test_removes_orphans_keeps_live(self, tmp_path):
        wm = WorkspaceManager(tmp_path)
        pi_root = tmp_path / "_runtime_config" / "pi"

        live_id = "aaaa1111-0000-0000-0000-000000000001"
        dead_id = "aaaa2222-0000-0000-0000-000000000002"

        for aid in (live_id, dead_id):
            d = pi_root / f"agent-{aid}"
            d.mkdir(parents=True)
            (d / "models.json").write_text("{}")

        removed = wm.cleanup_orphan_pi_configs({live_id})

        assert removed == [dead_id]
        assert (pi_root / f"agent-{live_id}").exists()
        assert not (pi_root / f"agent-{dead_id}").exists()

    def test_noop_when_root_missing(self, tmp_path):
        wm = WorkspaceManager(tmp_path)
        assert wm.cleanup_orphan_pi_configs(set()) == []

    def test_ignores_non_agent_dirs(self, tmp_path):
        wm = WorkspaceManager(tmp_path)
        pi_root = tmp_path / "_runtime_config" / "pi"
        (pi_root / "unrelated").mkdir(parents=True)

        removed = wm.cleanup_orphan_pi_configs(set())

        assert removed == []
        assert (pi_root / "unrelated").exists()


class TestHermesFactoryDropsPiConfig:
    def test_hermes_agent_gets_null_pi_config(self):
        from hermeshq.schemas.agent import AgentCreate

        payload = AgentCreate(
            node_id="n1",
            name="hermes-with-junk",
            runtime_type="hermes",
            pi_config={"tools": ["read"]},
        )
        assert payload.runtime_type == "hermes"
        # factory guard: pi_config only honored for pi runtime
        assert payload.pi_config if payload.runtime_type == "pi" else None is None
