from pathlib import Path

import pytest

from hermeshq.services.skill_transfer import (
    MAX_FILE_COUNT,
    SkillTransferError,
    SkillTransferService,
    _collect_files,
    _validate_name,
)


class TestValidateName:
    def test_simple_name(self) -> None:
        assert _validate_name("mi-skill") == "mi-skill"

    def test_nested_name(self) -> None:
        assert _validate_name("team/deep_skill") == "team/deep_skill"

    def test_rejects_traversal(self) -> None:
        for bad in ("../escape", "a/../../b", "..", ".", "a//b", "a/../b"):
            with pytest.raises(SkillTransferError):
                _validate_name(bad)

    def test_rejects_bad_chars(self) -> None:
        with pytest.raises(SkillTransferError):
            _validate_name("skill;rm -rf")

    def test_rejects_too_long(self) -> None:
        with pytest.raises(SkillTransferError):
            _validate_name("a" * 200)


class TestCollectFiles:
    def _make_skill(self, tmp: Path) -> Path:
        skill = tmp / "source" / "my-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# My skill\n\nDoes things", encoding="utf-8")
        (skill / "helper.py").write_text("print('hi')\n", encoding="utf-8")
        return skill

    def test_happy_path(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        files = _collect_files(skill)
        assert {f.name for f in files} == {"SKILL.md", "helper.py"}

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        link = skill / "escape"
        link.symlink_to(tmp_path)
        with pytest.raises(SkillTransferError, match="[Ss]ymlink"):
            _collect_files(skill)

    def test_rejects_binary(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        (skill / "blob.md").write_bytes(b"\x00\x01\x02binary")
        with pytest.raises(SkillTransferError, match="[Bb]inary"):
            _collect_files(skill)

    def test_rejects_disallowed_extension(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        (skill / "run.exe").write_text("MZ", encoding="utf-8")
        with pytest.raises(SkillTransferError, match="not allowed"):
            _collect_files(skill)

    def test_rejects_oversize_file(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        (skill / "big.txt").write_bytes(b"x" * (512 * 1024 + 1))
        with pytest.raises(SkillTransferError, match="too large"):
            _collect_files(skill)

    def test_rejects_too_many_files(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path)
        for i in range(MAX_FILE_COUNT + 1):
            (skill / f"f{i}.txt").write_text("x", encoding="utf-8")
        with pytest.raises(SkillTransferError, match="too many"):
            _collect_files(skill)


class TestTransfer:
    def _service(self) -> SkillTransferService:
        class FakeManager:
            def build_hermes_home(self, workspace: str) -> Path:
                return Path(workspace) / ".hermes"

        return SkillTransferService(FakeManager())  # type: ignore[arg-type]

    def _agent(self, tmp: Path, name: str, with_skill: bool = True) -> object:
        if with_skill:
            skills = tmp / name / ".hermes" / "skills" / "seed-skill"
            skills.mkdir(parents=True)
            (skills / "SKILL.md").write_text("# Seed\n\nSeed skill description here", encoding="utf-8")
        else:
            (tmp / name / ".hermes" / "skills").mkdir(parents=True)
        return type("A", (), {"id": name, "workspace_path": str(tmp / name), "is_archived": False})()

    def test_transfer_copies_skill(self, tmp_path: Path) -> None:
        import asyncio

        source = self._agent(tmp_path, "src")
        target = self._agent(tmp_path, "dst", with_skill=False)
        service = self._service()
        outcome = asyncio.run(
            service.transfer(
                source_agent=source,  # type: ignore[arg-type]
                target_agent=target,  # type: ignore[arg-type]
                skill_path="seed-skill",
            )
        )
        assert outcome.files == 1
        assert outcome.name == "seed-skill"
        copied = tmp_path / "dst" / ".hermes" / "skills" / "seed-skill" / "SKILL.md"
        assert copied.read_text(encoding="utf-8").startswith("# Seed")
        assert "Seed skill description here" in outcome.description

    def test_collision_requires_overwrite(self, tmp_path: Path) -> None:
        source = self._agent(tmp_path, "src")
        target = self._agent(tmp_path, "dst")
        existing = tmp_path / "dst" / ".hermes" / "skills" / "seed-skill"
        existing.mkdir(parents=True, exist_ok=True)
        (existing / "SKILL.md").write_text("# Existing", encoding="utf-8")
        service = self._service()
        import asyncio

        with pytest.raises(SkillTransferError) as exc_info:
            asyncio.run(
                service.transfer(
                    source_agent=source,  # type: ignore[arg-type]
                    target_agent=target,  # type: ignore[arg-type]
                    skill_path="seed-skill",
                )
            )
        assert exc_info.value.status_code == 409
        outcome = asyncio.run(
            service.transfer(
                source_agent=source,  # type: ignore[arg-type]
                target_agent=target,  # type: ignore[arg-type]
                skill_path="seed-skill",
                overwrite=True,
            )
        )
        assert outcome.files == 1

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        source = self._agent(tmp_path, "src")
        target = self._agent(tmp_path, "dst")
        service = self._service()
        import asyncio

        with pytest.raises(SkillTransferError):
            asyncio.run(
                service.transfer(
                    source_agent=source,  # type: ignore[arg-type]
                    target_agent=target,  # type: ignore[arg-type]
                    skill_path="../../etc",
                )
            )

    def test_same_agent_rejected(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path, "src")
        service = self._service()
        import asyncio

        with pytest.raises(SkillTransferError):
            asyncio.run(
                service.transfer(
                    source_agent=agent,  # type: ignore[arg-type]
                    target_agent=agent,  # type: ignore[arg-type]
                    skill_path="seed-skill",
                )
            )

    def test_managed_metadata_stripped(self, tmp_path: Path) -> None:
        source = self._agent(tmp_path, "src")
        (tmp_path / "src" / ".hermes" / "skills" / "seed-skill" / ".hermeshq-skill.json").write_text(
            '{"identifier": "catalog/thing"}', encoding="utf-8"
        )
        target = self._agent(tmp_path, "dst", with_skill=False)
        service = self._service()
        import asyncio

        outcome = asyncio.run(
            service.transfer(
                source_agent=source,  # type: ignore[arg-type]
                target_agent=target,  # type: ignore[arg-type]
                skill_path="seed-skill",
            )
        )
        assert outcome.files == 1
        assert not (tmp_path / "dst" / ".hermes" / "skills" / "seed-skill" / ".hermeshq-skill.json").exists()


class TestSshDestinationSchemas:
    def test_listen_port_bounds(self) -> None:
        import pytest as _pytest

        from hermeshq.schemas.ssh_destination import SshDestinationCreate

        with _pytest.raises(ValueError):
            SshDestinationCreate(name="x", host="10.0.0.1", listen_port=80)
        with _pytest.raises(ValueError):
            SshDestinationCreate(name="x", host="10.0.0.1", listen_port=65536)
        ok = SshDestinationCreate(name="x", host="10.0.0.1", listen_port=22001)
        assert ok.listen_port == 22001

    def test_host_validation(self) -> None:
        import pytest as _pytest

        from hermeshq.schemas.ssh_destination import SshDestinationCreate

        with _pytest.raises(ValueError):
            SshDestinationCreate(name="x", host="bad host", listen_port=22001)


class TestRelaySpecsGrouping:
    async def test_specs_grouped_by_agent(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from hermeshq.services.ssh_relay import _relay_specs_by_agent

        class D:
            def __init__(self, agent_id, listen, host):
                self.allowed_agent_id = agent_id
                self.listen_port = listen
                self.host = host
                self.port = 22
                self.active = True

        rows = MagicMock()
        rows.scalars.return_value.all.return_value = [
            D("agent-1", 22001, "a.example"),
            D("agent-1", 22002, "b.example"),
        ]
        db = MagicMock()
        db.execute = AsyncMock(return_value=rows)
        specs = await _relay_specs_by_agent(db)
        assert specs == {
            "agent-1": [
                {"listen": 22001, "host": "a.example", "port": 22},
                {"listen": 22002, "host": "b.example", "port": 22},
            ]
        }
