"""Tests for Pi skills bridge — _sync_skills copies Hermes skills to .pi/skills/."""

from pathlib import Path
from unittest.mock import MagicMock

from hermeshq.services.pi_installation import PiInstallationManager


def _make_skill(root: Path, name: str, with_metadata: bool = True) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n\nInstructions for {name}.\n", encoding="utf-8")
    (skill_dir / "example.txt").write_text("example", encoding="utf-8")
    if with_metadata:
        (skill_dir / ".hermeshq-skill.json").write_text('{"name": "%s"}' % name, encoding="utf-8")


def _manager(tmp_path: Path) -> tuple[PiInstallationManager, Path]:
    workspace = tmp_path / "workspaces" / "agent-1"
    hermes_skills = workspace / ".hermes" / "skills" / "hermeshq-managed"
    hermes_skills.mkdir(parents=True)

    wm = MagicMock()
    wm.build_workspace_path.return_value = workspace
    wm.build_pi_config_path.return_value = tmp_path / "pi-config"

    return PiInstallationManager(MagicMock(), wm), hermes_skills


class TestSyncSkills:
    def test_copies_skills_with_extra_files(self, tmp_path):
        manager, hermes_skills = _manager(tmp_path)
        _make_skill(hermes_skills, "my-skill")

        pi_home = tmp_path / "pi-config"
        pi_home.mkdir()

        manager._sync_skills(MagicMock(), pi_home)

        copied = pi_home / "skills" / "my-skill"
        assert (copied / "SKILL.md").exists()
        assert (copied / "example.txt").exists()

    def test_drops_hermes_metadata_sidecar(self, tmp_path):
        manager, hermes_skills = _manager(tmp_path)
        _make_skill(hermes_skills, "tracked")

        pi_home = tmp_path / "pi-config"
        pi_home.mkdir()

        manager._sync_skills(MagicMock(), pi_home)

        assert not (pi_home / "skills" / "tracked" / ".hermeshq-skill.json").exists()

    def test_noop_when_no_hermes_skills(self, tmp_path):
        manager, _ = _manager(tmp_path)

        pi_home = tmp_path / "pi-config"
        pi_home.mkdir()

        manager._sync_skills(MagicMock(), pi_home)

        assert (pi_home / "skills").exists()
        assert list((pi_home / "skills").iterdir()) == []

    def test_refreshes_existing_copy(self, tmp_path):
        manager, hermes_skills = _manager(tmp_path)
        _make_skill(hermes_skills, "evolving")

        pi_home = tmp_path / "pi-config"
        stale = pi_home / "skills" / "evolving"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("old content", encoding="utf-8")

        manager._sync_skills(MagicMock(), pi_home)

        assert "Instructions for evolving" in (stale / "SKILL.md").read_text(encoding="utf-8")

    def test_ignores_dirs_without_skill_md(self, tmp_path):
        manager, hermes_skills = _manager(tmp_path)
        (hermes_skills / "not-a-skill").mkdir()

        pi_home = tmp_path / "pi-config"
        pi_home.mkdir()

        manager._sync_skills(MagicMock(), pi_home)

        assert not (pi_home / "skills" / "not-a-skill").exists()
