from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.hermes_installation.manager import HermesInstallationManager


def _manager_with_provider(base_url: str | None) -> HermesInstallationManager:
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(base_url=base_url))
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
    manager = MagicMock(spec=HermesInstallationManager)
    manager.session_factory = session_factory
    manager._uses_custom_openai_provider = MagicMock(return_value=True)
    manager._normalize_openai_compatible_base_url = lambda value: HermesInstallationManager._normalize_openai_compatible_base_url(
        manager, value
    )
    return manager


def _agent(base_url: str | None, provider: str = "puget") -> SimpleNamespace:
    return SimpleNamespace(base_url=base_url, provider=provider)


class TestProviderBaseUrlFallback:
    async def test_agent_base_url_wins(self) -> None:
        manager = _manager_with_provider("https://catalog.example.com/v1")
        agent = _agent("https://agent.example.com/v1")
        result = await HermesInstallationManager._effective_provider_base_url(manager, agent)
        assert result == "https://agent.example.com/v1"
        manager.session_factory.assert_not_called()

    async def test_falls_back_to_catalog_when_agent_empty(self) -> None:
        manager = _manager_with_provider("https://puget.sixmanager.io/v1")
        agent = _agent(None)
        result = await HermesInstallationManager._effective_provider_base_url(manager, agent)
        assert result == "https://puget.sixmanager.io/v1"

    async def test_empty_everywhere_keeps_default(self) -> None:
        manager = _manager_with_provider(None)
        agent = _agent("")
        result = await HermesInstallationManager._effective_provider_base_url(manager, agent)
        assert result == "https://api.openai.com/v1"

    async def test_agent_base_url_without_provider_slug(self) -> None:
        manager = _manager_with_provider("https://catalog.example.com/v1")
        agent = _agent(None, provider=None)
        result = await HermesInstallationManager._effective_provider_base_url(manager, agent)
        assert result == "https://api.openai.com/v1"
        manager.session_factory.assert_not_called()
