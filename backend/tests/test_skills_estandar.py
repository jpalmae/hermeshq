import unittest

from hermeshq.services.runtime_profiles import (
    STANDARD_MANAGED_SKILLS,
    missing_standard_skills,
    teams_for_mail_agents,
)


class TestSkillsEstandar(unittest.TestCase):
    def test_an_agent_with_the_documents_toolset_gets_every_missing_standard_skill(self):
        faltan = missing_standard_skills(["file", "hermeshq_documentos"], ["local/crear-skill", "local/sharepoint"])
        self.assertEqual(set(faltan), set(STANDARD_MANAGED_SKILLS) - {"local/crear-skill"})
        self.assertNotIn("local/sharepoint", faltan)

    def test_nothing_is_added_when_the_agent_already_has_them_all(self):
        self.assertEqual(missing_standard_skills(["hermeshq_documentos"], list(STANDARD_MANAGED_SKILLS)), [])

    def test_agents_without_the_documents_toolset_are_left_alone(self):
        self.assertEqual(missing_standard_skills(["file"], []), [])
        self.assertEqual(missing_standard_skills(None, None), [])

    def test_the_new_skills_exist_as_managed_templates(self):
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[1] / "hermeshq" / "skill_templates"
        for identificador in STANDARD_MANAGED_SKILLS:
            self.assertTrue((raiz / identificador.split("/", 1)[1] / "SKILL.md").is_file(), identificador)


class TestTeamsParaAgentesConCorreo(unittest.TestCase):
    def test_an_agent_with_mail_gets_the_integration_the_toolset_and_the_skill(self):
        cambios = teams_for_mail_agents({"ms365-mail": {}}, ["file"], [], ["local/ms365-mail"])
        self.assertEqual(cambios["integration_configs"], {"ms365-mail": {}, "ms365-teams": {}})
        self.assertEqual(cambios["enabled_toolsets"], ["file", "hermeshq_ms365_teams"])
        self.assertEqual(cambios["skills"], ["local/ms365-mail", "local/ms365-teams"])

    def test_nothing_changes_when_it_already_has_everything(self):
        self.assertEqual(
            teams_for_mail_agents(
                {"ms365-mail": {}, "ms365-teams": {}}, ["hermeshq_ms365_teams"], [], ["local/ms365-teams"]
            ),
            {},
        )

    def test_agents_without_mail_are_left_alone(self):
        self.assertEqual(teams_for_mail_agents({"sharepoint": {}}, [], [], []), {})
        self.assertEqual(teams_for_mail_agents(None, None, None, None), {})

    def test_an_administrator_can_opt_an_agent_out_by_disabling_the_toolset(self):
        self.assertEqual(teams_for_mail_agents({"ms365-mail": {}}, [], ["hermeshq_ms365_teams"], []), {})

    def test_only_the_missing_pieces_are_returned(self):
        cambios = teams_for_mail_agents({"ms365-mail": {}, "ms365-teams": {}}, ["hermeshq_ms365_teams"], [], [])
        self.assertEqual(list(cambios), ["skills"])


if __name__ == "__main__":
    unittest.main()
