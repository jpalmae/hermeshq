"""Tests for the M365 plugins' three-tier _task_user_id() resolution.

Regression coverage for the live-gateway user resolution added on top of the
pre-existing task-payload and HERMESHQ_RESOLVED_USER_ID tiers:
  1. Punctual tasks: thread_user_id/created_by_user_id from HERMESHQ_TASK_PAYLOAD.
  2. Live gateway messages: HERMES_SESSION_PLATFORM/HERMES_SESSION_USER_ID
     resolved via GET /control/resolve-channel-user (per-turn, contextvar-backed
     — safe under concurrent messages in the same gateway process, unlike the
     process-global fallback below).
  3. Single-user channel / cron: HERMESHQ_RESOLVED_USER_ID set at gateway start.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "hermeshq" / "integration_packages"

PLUGIN_MODULES = {
    "ms365_mail": PLUGIN_ROOT / "ms365-mail" / "plugin" / "__init__.py",
    "ms365_calendar": PLUGIN_ROOT / "ms365-calendar" / "plugin" / "__init__.py",
    "ms365_teams": PLUGIN_ROOT / "ms365-teams" / "plugin" / "__init__.py",
    "sharepoint": PLUGIN_ROOT / "sharepoint" / "plugin" / "__init__.py",
}

ENV_KEYS = (
    "HERMESHQ_TASK_PAYLOAD",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_USER_ID",
    "HERMESHQ_RESOLVED_USER_ID",
    "HERMESHQ_INTERNAL_API_URL",
    "HERMESHQ_AGENT_ID",
    "HERMESHQ_AGENT_TOKEN",
)


def _load_plugin(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"_test_{name}_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TaskUserIdResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HERMESHQ_INTERNAL_API_URL"] = "http://backend:8000/api/internal"
        os.environ["HERMESHQ_AGENT_ID"] = "agent-1"
        os.environ["HERMESHQ_AGENT_TOKEN"] = "token-1"

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_tier1_task_payload_wins_without_network_call(self):
        for name, path in PLUGIN_MODULES.items():
            with self.subTest(plugin=name):
                module = _load_plugin(name, path)
                os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps(
                    {"metadata": {"thread_user_id": "user-from-task"}}
                )
                os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
                os.environ["HERMES_SESSION_USER_ID"] = "999"
                try:
                    with patch("urllib.request.urlopen") as mock_urlopen:
                        self.assertEqual(module._task_user_id(), "user-from-task")
                        mock_urlopen.assert_not_called()
                finally:
                    os.environ.pop("HERMESHQ_TASK_PAYLOAD", None)
                    os.environ.pop("HERMES_SESSION_PLATFORM", None)
                    os.environ.pop("HERMES_SESSION_USER_ID", None)

    def test_tier2_live_gateway_resolves_via_control_endpoint(self):
        for name, path in PLUGIN_MODULES.items():
            with self.subTest(plugin=name):
                module = _load_plugin(name, path)
                os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
                os.environ["HERMES_SESSION_USER_ID"] = "123456"
                body = json.dumps({"hermeshq_user_id": "resolved-user"}).encode("utf-8")
                try:
                    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)) as mock_urlopen:
                        self.assertEqual(module._task_user_id(), "resolved-user")
                        request = mock_urlopen.call_args[0][0]
                        self.assertIn("/control/resolve-channel-user", request.full_url)
                        self.assertIn("platform=telegram", request.full_url)
                        self.assertIn("sender_id=123456", request.full_url)
                        self.assertEqual(request.headers.get("X-hermeshq-agent-id"), "agent-1")
                finally:
                    os.environ.pop("HERMES_SESSION_PLATFORM", None)
                    os.environ.pop("HERMES_SESSION_USER_ID", None)

    def test_tier3_fallback_used_for_single_user_channel_or_cron(self):
        for name, path in PLUGIN_MODULES.items():
            with self.subTest(plugin=name):
                module = _load_plugin(name, path)
                os.environ["HERMESHQ_RESOLVED_USER_ID"] = "single-user-channel"
                try:
                    with patch("urllib.request.urlopen") as mock_urlopen:
                        self.assertEqual(module._task_user_id(), "single-user-channel")
                        mock_urlopen.assert_not_called()
                finally:
                    os.environ.pop("HERMESHQ_RESOLVED_USER_ID", None)

    def test_tier3_fallback_used_when_control_endpoint_has_no_match(self):
        for name, path in PLUGIN_MODULES.items():
            with self.subTest(plugin=name):
                module = _load_plugin(name, path)
                os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
                os.environ["HERMES_SESSION_USER_ID"] = "999999"
                os.environ["HERMESHQ_RESOLVED_USER_ID"] = "fallback-user"
                error = urllib.error.HTTPError("url", 404, "not found", None, None)
                try:
                    with patch("urllib.request.urlopen", side_effect=error):
                        self.assertEqual(module._task_user_id(), "fallback-user")
                finally:
                    os.environ.pop("HERMES_SESSION_PLATFORM", None)
                    os.environ.pop("HERMES_SESSION_USER_ID", None)
                    os.environ.pop("HERMESHQ_RESOLVED_USER_ID", None)

    def test_none_when_nothing_resolves(self):
        for name, path in PLUGIN_MODULES.items():
            with self.subTest(plugin=name):
                module = _load_plugin(name, path)
                with patch("urllib.request.urlopen") as mock_urlopen:
                    self.assertIsNone(module._task_user_id())
                    mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
