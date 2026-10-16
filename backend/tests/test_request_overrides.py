import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from hermeshq.routers.agents_shared import _apply_agent_runtime_behavior_settings
from hermeshq.services.hermes_runtime import HermesRuntime


def _load(content=None):
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        if content is not None:
            (home / "request_overrides.json").write_text(content, encoding="utf-8")
        return HermesRuntime._load_request_overrides(home)


class RequestOverridesTests(unittest.TestCase):
    def test_missing_file_means_no_overrides(self):
        self.assertEqual(_load(), {})

    def test_valid_object_is_returned(self):
        content = '{"extra_body": {"chat_template_kwargs": {"enable_thinking": false}}}'
        self.assertEqual(_load(content), {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})

    def test_invalid_json_is_ignored(self):
        self.assertEqual(_load("{not json"), {})

    def test_non_object_json_is_ignored(self):
        self.assertEqual(_load("[1, 2, 3]"), {})
        self.assertEqual(_load('"texto"'), {})

    def test_directory_instead_of_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "request_overrides.json").mkdir()
            self.assertEqual(HermesRuntime._load_request_overrides(home), {})


class ThinkingModeTests(unittest.TestCase):
    def test_default_sends_no_override(self):
        self.assertEqual(HermesRuntime._thinking_overrides(None), {})
        self.assertEqual(HermesRuntime._thinking_overrides("inherit"), {})

    def test_off_disables_thinking_for_the_model_server(self):
        self.assertEqual(
            HermesRuntime._thinking_overrides("off"),
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        )

    def test_on_enables_thinking_explicitly(self):
        self.assertEqual(
            HermesRuntime._thinking_overrides("on"),
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
        )

    def test_agent_setting_and_file_are_merged_with_file_taking_precedence(self):
        agent = SimpleNamespace(thinking_mode="off")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "request_overrides.json").write_text(
                '{"extra_body": {"chat_template_kwargs": {"enable_thinking": true, "x": 1}}, "service_tier": "fast"}',
                encoding="utf-8",
            )
            merged = HermesRuntime._build_request_overrides(agent, home)
        self.assertEqual(
            merged,
            {
                "parallel_tool_calls": False,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": True, "x": 1}},
                "service_tier": "fast",
            },
        )

    def test_agent_without_the_attribute_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                HermesRuntime._build_request_overrides(SimpleNamespace(), Path(tmp)), {"parallel_tool_calls": False}
            )

    def test_every_agent_gets_sequential_tool_calls_unless_its_file_says_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(
                HermesRuntime._build_request_overrides(SimpleNamespace(thinking_mode="on"), home)["parallel_tool_calls"]
            )
            (home / "request_overrides.json").write_text('{"parallel_tool_calls": true}', encoding="utf-8")
            self.assertTrue(HermesRuntime._build_request_overrides(SimpleNamespace(), home)["parallel_tool_calls"])

    def test_merge_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        HermesRuntime._merge_overrides(base, {"a": {"c": 2}})
        self.assertEqual(base, {"a": {"b": 1}})


class ThinkingModeNormalizationTests(unittest.TestCase):
    def _apply(self, value):
        agent = SimpleNamespace(thinking_mode="stale")
        _apply_agent_runtime_behavior_settings(agent, {"thinking_mode": value})
        return agent.thinking_mode

    def test_valid_values_are_normalized(self):
        self.assertEqual(self._apply("off"), "off")
        self.assertEqual(self._apply(" ON "), "on")

    def test_inherit_and_empty_clear_the_setting(self):
        self.assertIsNone(self._apply("inherit"))
        self.assertIsNone(self._apply(""))
        self.assertIsNone(self._apply(None))

    def test_invalid_value_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._apply("maybe")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_untouched_when_not_in_payload(self):
        agent = SimpleNamespace(thinking_mode="off")
        _apply_agent_runtime_behavior_settings(agent, {})
        self.assertEqual(agent.thinking_mode, "off")


class MigrationChainTests(unittest.TestCase):
    def test_alembic_has_a_single_head(self):
        versions = Path(__file__).resolve().parents[1] / "hermeshq" / "alembic" / "versions"
        revisions, downs = set(), set()
        for path in versions.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            revision = re.search(r'^revision(?:: str)? *= *"([^"]+)"', text, re.M)
            down = re.search(r"^down_revision[^=]*=(.+)$", text, re.M)
            if revision:
                revisions.add(revision.group(1))
            if down:
                downs.update(re.findall(r'"([^"]+)"', down.group(1)))
        self.assertEqual(len(revisions - downs), 1, sorted(revisions - downs))


if __name__ == "__main__":
    unittest.main()


class TestScheduledTasksThinking(unittest.TestCase):
    def _task(self, scheduled):
        return SimpleNamespace(metadata_json={"scheduled": True} if scheduled else {"source": "public_chat"})

    def _build(self, mode, task, setting="off"):
        from unittest.mock import patch

        with patch("hermeshq.config.get_settings", return_value=SimpleNamespace(scheduled_tasks_thinking=setting)):
            return HermesRuntime._build_request_overrides(
                SimpleNamespace(thinking_mode=mode), Path("/nonexistent"), task
            )

    def test_scheduled_task_runs_without_thinking_even_if_agent_has_it_on(self):
        out = self._build("on", self._task(True))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)

    def test_scheduled_task_with_inherit_agent_also_turns_it_off(self):
        out = self._build(None, self._task(True))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)

    def test_chat_task_keeps_the_agent_setting(self):
        out = self._build("on", self._task(False))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], True)

    def test_inherit_setting_leaves_scheduled_tasks_with_the_agent_mode(self):
        out = self._build("on", self._task(True), setting="inherit")
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], True)

    def test_without_a_task_behaviour_is_unchanged(self):
        out = self._build("off", None)
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)
