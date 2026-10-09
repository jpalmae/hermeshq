"""Unit tests for the Desktop Gateway bridge (service + router)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermeshq.services.desktop_gateway import GatewayHandle, desktop_gateway_service_factory


class TestGatewayHandle:
    def test_base_and_ws_urls(self):
        process = MagicMock()
        process.returncode = None
        handle = GatewayHandle(agent_id="a1", port=9310, process=process, inner_token="tok123")
        assert handle.base_url == "http://127.0.0.1:9310"
        assert handle.ws_url == "ws://127.0.0.1:9310/api/ws?token=tok123"

    def test_touch_updates_last_active(self):
        process = MagicMock()
        process.returncode = None
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        handle.last_active = time.monotonic() - 100
        handle.touch()
        assert time.monotonic() - handle.last_active < 5


class TestDesktopGatewayService:
    def _make_service(self):
        return desktop_gateway_service_factory(session_factory=MagicMock(), installation_manager=MagicMock())

    def test_get_handle_returns_none_when_missing(self):
        service = self._make_service()
        assert service.get_handle("nope") is None

    def test_get_handle_cleans_up_dead_process(self):
        service = self._make_service()
        process = MagicMock()
        process.returncode = 1
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        service._gateways["a1"] = handle
        assert service.get_handle("a1") is None
        assert "a1" not in service._gateways

    def test_track_ws_connection(self):
        service = self._make_service()
        process = MagicMock()
        process.returncode = None
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        service._gateways["a1"] = handle
        service.track_ws_connection("a1", 1)
        assert handle.ws_connections == 1
        service.track_ws_connection("a1", -1)
        assert handle.ws_connections == 0

    def test_pick_free_port_returns_callable_port(self):
        service = self._make_service()
        port = service._pick_free_port()
        assert port is not None
        assert 9300 <= port <= 9499

    @pytest.mark.asyncio
    async def test_stop_agent_terminates_process(self):
        service = self._make_service()
        process = AsyncMock()
        process.returncode = None
        process.wait = AsyncMock()
        process.terminate = MagicMock()
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        service._gateways["a1"] = handle
        await service.stop_agent("a1")
        process.terminate.assert_called_once()
        assert "a1" not in service._gateways

    @pytest.mark.asyncio
    async def test_reaper_stops_idle_gateways(self):
        service = self._make_service()
        process = AsyncMock()
        process.returncode = None
        process.wait = AsyncMock()
        process.terminate = MagicMock()
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        handle.last_active = time.monotonic() - (31 * 60)
        service._gateways["a1"] = handle
        await service._reaper_single_pass()
        process.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaper_keeps_active_ws_connections(self):
        service = self._make_service()
        process = AsyncMock()
        process.returncode = None
        process.terminate = MagicMock()
        handle = GatewayHandle(agent_id="a1", port=9310, process=process)
        handle.ws_connections = 1
        handle.last_active = time.monotonic() - (31 * 60)
        service._gateways["a1"] = handle
        await service._reaper_single_pass()
        process.terminate.assert_not_called()


class TestDesktopAuthHelpers:
    def test_extract_session_token_prefers_header(self):
        from hermeshq.routers.desktop_gateway import _extract_session_token

        request = MagicMock()
        request.headers = {
            "X-Hermes-Session-Token": "tok-1",
            "Authorization": "Bearer tok-2",
        }
        request.cookies = {}
        assert _extract_session_token(request) == "tok-1"

    def test_extract_session_token_falls_back_to_bearer(self):
        from hermeshq.routers.desktop_gateway import _extract_session_token

        request = MagicMock()
        request.headers = {"Authorization": "Bearer tok-2"}
        request.cookies = {}
        assert _extract_session_token(request) == "tok-2"

    def test_extract_session_token_falls_back_to_cookie(self):
        from hermeshq.routers.desktop_gateway import _extract_session_token

        request = MagicMock()
        request.headers = {}
        request.cookies = {"hermeshq_token": "tok-3"}
        assert _extract_session_token(request) == "tok-3"

    def test_forwardable_headers_strips_sensitive_and_injects_inner_token(self):
        from hermeshq.routers.desktop_gateway import _forwardable_headers

        request = MagicMock()
        request.headers = {
            "host": "x",
            "authorization": "Bearer x",
            "x-hermes-session-token": "x",
            "content-type": "application/json",
            "cookie": "a=b",
        }
        headers = _forwardable_headers(request, "inner-tok")
        assert "host" not in headers
        assert "authorization" not in headers
        assert "cookie" not in headers
        assert headers["content-type"] == "application/json"
        assert headers["X-Hermes-Session-Token"] == "inner-tok"


class TestDesktopGuardPluginCatalog:
    def test_guard_excluded_by_default(self):
        from hermeshq.services.managed_capabilities import list_managed_plugins

        names = {p["template_dir"] for p in list_managed_plugins([])}
        assert "hermeshq_guard" not in names

    def test_guard_included_when_desktop_access(self):
        from hermeshq.services.managed_capabilities import list_managed_plugins

        names = {p["template_dir"] for p in list_managed_plugins([], include_desktop_guard=True)}
        assert "hermeshq_guard" in names


class TestGuardPluginUnit:
    def test_pre_tool_call_blocks_on_denied(self, monkeypatch):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "hermeshq_guard_test",
            Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_guard" / "__init__.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        monkeypatch.setattr(module, "_evaluate", lambda tool, tool_input: {"action": "block", "message": "nope"})
        result = module._on_pre_tool_call("Bash", {"command": "rm -rf /"})
        assert result == {"action": "block", "message": "nope"}

    def test_pre_tool_call_allows_when_evaluation_passes(self, monkeypatch):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "hermeshq_guard_test2",
            Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_guard" / "__init__.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        monkeypatch.setattr(module, "_evaluate", lambda tool, tool_input: None)
        assert module._on_pre_tool_call("Read", {"path": "/tmp"}) is None

    def test_pre_tool_call_without_tool_name(self, monkeypatch):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "hermeshq_guard_test3",
            Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_guard" / "__init__.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        called = []
        monkeypatch.setattr(module, "_evaluate", lambda tool, tool_input: called.append(tool) or None)
        assert module._on_pre_tool_call("", None) is None
        assert called == []


def test_ws_pump_propagates_messages():
    from hermeshq.routers.desktop_gateway import _ws_pump_both_directions

    client_ws = MagicMock()
    messages = ['{"jsonrpc":"2.0","method":"ping"}']
    received_by_client = []

    async def receive_text():
        while not messages:
            await asyncio.sleep(0.05)
        return messages.pop(0)

    client_ws.receive_text = receive_text

    async def send_text(message):
        received_by_client.append(message)

    client_ws.send_text = send_text

    sent_upstream = []

    class FakeUpstream:
        async def send(self, message):
            sent_upstream.append(message)

        async def recv(self):
            await asyncio.sleep(0.05)
            return '{"jsonrpc":"2.0","result":"pong"}'

    upstream = FakeUpstream()

    async def run():
        task = asyncio.create_task(_ws_pump_both_directions(client_ws, upstream))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert sent_upstream == ['{"jsonrpc":"2.0","method":"ping"}']
    assert '{"jsonrpc":"2.0","result":"pong"}' in received_by_client
