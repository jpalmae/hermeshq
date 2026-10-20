import re
import unittest
from pathlib import Path

from hermeshq.services.managed_capabilities import fetch_local_skill_bundle, skill_templates_root

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "hermeshq"
NOT_TOOLS = {"teams_join_url"}
TOOL_TOKEN = re.compile(r"\b((?:ms365|sharepoint|hq|teams|onedrive)_[a-z0-9_]+)\b")


def _registered_tools() -> set[str]:
    tools: set[str] = set()
    plugin_files = list(PACKAGE_ROOT.glob("integration_packages/*/plugin/__init__.py"))
    plugin_files += list(PACKAGE_ROOT.glob("plugin_templates/*/__init__.py"))
    for path in plugin_files:
        text = path.read_text(encoding="utf-8")
        tools |= set(re.findall(r'name="([a-z0-9_]+)"', text))
        tools |= set(re.findall(r'"name": "([a-z0-9_]+)"', text))
    return tools


def _all_skill_files() -> list[Path]:
    files = list(PACKAGE_ROOT.glob("integration_packages/*/*/SKILL.md"))
    files += list(skill_templates_root().glob("*/SKILL.md"))
    return files


class TestManagedSkills(unittest.TestCase):
    def test_every_template_has_matching_frontmatter_and_is_small(self):
        for skill_md in skill_templates_root().glob("*/SKILL.md"):
            text = skill_md.read_text(encoding="utf-8")
            match = re.match(r"---\nname: ([^\n]+)\ndescription: \"?(.+?)\"?\n---\n", text, re.S)
            self.assertIsNotNone(match, skill_md)
            self.assertEqual(match.group(1).strip(), skill_md.parent.name)
            self.assertGreater(len(match.group(2)), 30)
            self.assertLess(len(text.encode("utf-8")), 9000, f"{skill_md.parent.name} pesa demasiado")

    def test_skills_only_mention_tools_that_exist(self):
        tools = _registered_tools()
        self.assertGreater(len(tools), 30)
        problems = {}
        for skill_md in _all_skill_files():
            mentioned = {t for t in TOOL_TOKEN.findall(skill_md.read_text(encoding="utf-8")) if not t.endswith("_")}
            missing = sorted(mentioned - tools - NOT_TOOLS)
            if missing:
                problems[str(skill_md.relative_to(PACKAGE_ROOT))] = missing
        self.assertEqual(problems, {})

    def test_new_skills_are_installable_as_local_bundles(self):
        for name in ("catalogo-de-capacidades", "crear-skill"):
            bundle = fetch_local_skill_bundle(f"local/{name}")
            self.assertIsNotNone(bundle, name)
            self.assertIn("SKILL.md", bundle["files"])
        self.assertIn("references/plantilla.md", fetch_local_skill_bundle("local/crear-skill")["files"])

    def test_catalog_covers_the_key_capabilities(self):
        text = (skill_templates_root() / "catalogo-de-capacidades" / "SKILL.md").read_text(encoding="utf-8")
        for expected in (
            "ms365_find_person",
            "ms365_mail_folders",
            "sharepoint_share_file",
            "ms365_calendar_update_event",
            "hq_schedule_request",
            "attachments",
            "online_meeting",
        ):
            self.assertIn(expected, text)

    def test_creator_skill_enforces_the_budget_rules(self):
        text = (skill_templates_root() / "crear-skill" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("6 herramientas", text)
        self.assertIn("catalogo-de-capacidades", text)
        self.assertIn("8 KB", text)
