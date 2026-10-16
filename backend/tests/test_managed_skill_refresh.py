import tempfile
import unittest
from pathlib import Path

from hermeshq.services.hermes_installation.manager import managed_skill_is_current


class TestManagedSkillRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_identical_copy_is_current(self):
        (self.dir / "SKILL.md").write_text("hola", encoding="utf-8")
        self.assertTrue(managed_skill_is_current(self.dir, {"SKILL.md": "hola"}))

    def test_changed_repo_content_marks_stale(self):
        (self.dir / "SKILL.md").write_text("viejo", encoding="utf-8")
        self.assertFalse(managed_skill_is_current(self.dir, {"SKILL.md": "nuevo"}))

    def test_locally_edited_copy_is_restored(self):
        (self.dir / "SKILL.md").write_text("editado por el agente", encoding="utf-8")
        self.assertFalse(managed_skill_is_current(self.dir, {"SKILL.md": "original"}))

    def test_missing_file_marks_stale(self):
        (self.dir / "SKILL.md").write_text("x", encoding="utf-8")
        self.assertFalse(managed_skill_is_current(self.dir, {"SKILL.md": "x", "scripts/a.py": "print()"}))

    def test_binary_files_are_compared_as_bytes(self):
        (self.dir / "logo.png").write_bytes(b"\x89PNG")
        self.assertTrue(managed_skill_is_current(self.dir, {"logo.png": b"\x89PNG"}))
        self.assertFalse(managed_skill_is_current(self.dir, {"logo.png": b"\x89PNX"}))
