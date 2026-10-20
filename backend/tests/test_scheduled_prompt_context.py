import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from hermeshq.services.cron_schedule import build_scheduled_prompt, current_time_context


class CurrentTimeContextTests(unittest.TestCase):
    def test_chile_summer_shows_local_offset_and_utc(self):
        now = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
        text = current_time_context("America/Santiago", now)
        self.assertIn("martes 2026-10-06 17:30 America/Santiago (UTC-03:00)", text)
        self.assertIn("= 2026-10-06T20:30:00Z", text)

    def test_chile_winter_offset_is_minus_four(self):
        now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        self.assertIn("(UTC-04:00)", current_time_context("America/Santiago", now))

    def test_legacy_utc_schedule_is_labelled_utc(self):
        now = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
        text = current_time_context("UTC", now)
        self.assertIn("2026-10-06 20:30 UTC (UTC+00:00)", text)

    def test_date_rolls_over_with_the_timezone(self):
        now = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
        self.assertIn("martes 2026-10-06 23:00", current_time_context("America/Santiago", now))

    def test_build_prompt_keeps_original_text_first_and_appends_context(self):
        schedule = SimpleNamespace(prompt="Ejecuta la skill informe.  \n", timezone="America/Santiago")
        now = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)
        prompt = build_scheduled_prompt(schedule, now)
        self.assertTrue(prompt.startswith("Ejecuta la skill informe.\n\n[Hora actual:"))
        self.assertTrue(prompt.endswith("saber la hora.]"))

    def test_stored_schedule_prompt_is_not_modified(self):
        schedule = SimpleNamespace(prompt="Ejecuta", timezone="America/Santiago")
        build_scheduled_prompt(schedule)
        self.assertEqual(schedule.prompt, "Ejecuta")


class ToolDescriptionTests(unittest.TestCase):
    def test_document_tools_tell_agents_not_to_script_them(self):
        import importlib.util
        from pathlib import Path
        from unittest.mock import MagicMock

        root = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"
        for slug, marker in (
            ("hermeshq_pdf", "reportlab"),
            ("hermeshq_docx", "python-docx"),
            ("hermeshq_xlsx", "openpyxl"),
            ("hermeshq_pptx", "python-pptx"),
        ):
            with self.subTest(plugin=slug):
                spec = importlib.util.spec_from_file_location(f"{slug}_desc", root / slug / "__init__.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                ctx = MagicMock()
                module.register(ctx)
                descriptions = " ".join(c.kwargs["schema"]["description"] for c in ctx.register_tool.call_args_list)
                self.assertIn("ALWAYS use this tool", descriptions)
                self.assertIn(marker, descriptions)


if __name__ == "__main__":
    unittest.main()
