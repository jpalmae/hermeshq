"""Tests for the Teams streaming hotfix (edit_message injection)."""

from __future__ import annotations

import textwrap

from hermeshq.services.hermes_version_manager import HermesVersionManager


def _manager(tmp_path) -> HermesVersionManager:
    from hermeshq.config import get_settings

    manager = HermesVersionManager.__new__(HermesVersionManager)
    manager.root = tmp_path
    manager.settings = get_settings()
    return manager


def _fake_adapter(root, version: str, content: str):
    adapter_dir = root / version / ".venv" / "lib" / "python3.11" / "site-packages" / "plugins" / "platforms" / "teams"
    adapter_dir.mkdir(parents=True)
    adapter = adapter_dir / "adapter.py"
    adapter.write_text(content, encoding="utf-8")
    return adapter


BASE_ADAPTER = textwrap.dedent(
    """
    class TeamsAdapter(BaseAdapter):
        def __init__(self, config):
            self._conv_refs = {}
            self._client_id = None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

    """
    + HermesVersionManager._TEAMS_SEND_TYPING_NEEDLE
    + "\n            pass\n"
)


class TestTeamsStreamingHotfix:
    def test_applies_edit_message_before_send_typing(self, tmp_path):
        manager = _manager(tmp_path)
        adapter = _fake_adapter(tmp_path, "0.15.2", BASE_ADAPTER)
        manager._apply_teams_streaming_hotfix("0.15.2")
        patched = adapter.read_text(encoding="utf-8")
        assert manager._TEAMS_EDIT_HOTFIX_MARKER in patched
        assert patched.index("async def edit_message") < patched.index("async def send_typing")

    def test_idempotent_marker_skips(self, tmp_path):
        manager = _manager(tmp_path)
        already = BASE_ADAPTER + f"# {manager._TEAMS_EDIT_HOTFIX_MARKER}\n"
        adapter = _fake_adapter(tmp_path, "0.15.2", already)
        manager._apply_teams_streaming_hotfix("0.15.2")
        assert adapter.read_text(encoding="utf-8") == already

    def test_needle_missing_does_not_touch_file(self, tmp_path):
        manager = _manager(tmp_path)
        different = "class Other:\n    pass\n"
        adapter = _fake_adapter(tmp_path, "0.15.2", different)
        manager._apply_teams_streaming_hotfix("0.15.2")
        assert adapter.read_text(encoding="utf-8") == different

    def test_missing_adapter_path_is_noop(self, tmp_path):
        manager = _manager(tmp_path)
        manager._apply_teams_streaming_hotfix("9.9.9")

    def test_edit_method_is_valid_python(self, tmp_path):
        import ast

        wrapped = "class Dummy:\n" + HermesVersionManager._TEAMS_EDIT_METHOD
        parsed = ast.parse(wrapped)
        methods = [n.name for n in ast.walk(parsed) if isinstance(n, ast.AsyncFunctionDef)]
        assert "edit_message" in methods
        assert "v3/conversations" in HermesVersionManager._TEAMS_EDIT_METHOD
        assert "client_credentials" in HermesVersionManager._TEAMS_EDIT_METHOD
