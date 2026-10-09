from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hermeshq.schemas.managed_integration import ManagedIntegrationActionRequest, ManagedIntegrationTestRequest
from hermeshq.services import managed_integration_actions, managed_integration_health
from hermeshq.services.managed_integration_actions import (
    ManagedIntegrationActionError,
    run_managed_integration_action,
)
from hermeshq.services.managed_integration_health import ManagedIntegrationTestError
from hermeshq.services.managed_integration_health import test_managed_integration as run_managed_integration_test


def _integration() -> dict:
    return {
        "slug": "gamma-app",
        "actions": [{"slug": "list_themes"}],
        "defaults": {"base_url": "https://public-api.gamma.app/v1.0"},
        "fields": [
            {"name": "api_key_ref", "kind": "secret_ref"},
            {"name": "base_url", "kind": "url"},
        ],
        "package_root": "/tmp/gamma-app",
    }


def test_managed_integration_payloads_reject_credential_configuration() -> None:
    with pytest.raises(ValidationError):
        ManagedIntegrationTestRequest(config={"api_key_ref": "victim"})
    with pytest.raises(ValidationError):
        ManagedIntegrationActionRequest(config={"base_url": "https://attacker.invalid"})


@pytest.mark.asyncio
async def test_action_uses_persisted_config_and_scopes_secret_resolution(monkeypatch) -> None:
    integration = _integration()
    agent = SimpleNamespace(
        integration_configs={
            "gamma-app": {
                "api_key_ref": "agent-gamma-key",
                "base_url": "https://public-api.gamma.app/v1.0",
            }
        }
    )
    resolved_refs: list[str] = []
    captured: dict = {}

    async def resolve_secret(secret_ref: str) -> str:
        resolved_refs.append(secret_ref)
        return f"value:{secret_ref}"

    async def fake_action(
        action_slug,
        *,
        agent,
        config,
        arguments,
        resolve_secret,
        workspaces_root,
        package_root=None,
    ):
        captured.update(config=config, arguments=arguments)
        captured["untrusted_secret"] = await resolve_secret("victim-key")
        captured["trusted_secret"] = await resolve_secret(str(config["api_key_ref"]))
        return True, "ok", None

    monkeypatch.setattr(managed_integration_actions, "get_managed_integration", lambda *_args, **_kwargs: integration)
    monkeypatch.setattr(
        managed_integration_actions,
        "_load_actions_module",
        lambda _integration: SimpleNamespace(run_action=fake_action),
    )

    result = await run_managed_integration_action(
        agent,
        "gamma-app",
        "list_themes",
        {"query": "quarterly themes"},
        ["gamma-app"],
        resolve_secret,
    )

    assert result == (True, "ok", None)
    assert captured["config"] == agent.integration_configs["gamma-app"]
    assert captured["arguments"] == {"query": "quarterly themes"}
    assert captured["untrusted_secret"] is None
    assert captured["trusted_secret"] == "value:agent-gamma-key"
    assert resolved_refs == ["agent-gamma-key"]


@pytest.mark.asyncio
async def test_action_rejects_attempts_to_override_credentials_or_endpoints(monkeypatch) -> None:
    integration = _integration()
    agent = SimpleNamespace(
        integration_configs={
            "gamma-app": {
                "api_key_ref": "agent-gamma-key",
                "base_url": "https://public-api.gamma.app/v1.0",
            }
        }
    )
    monkeypatch.setattr(managed_integration_actions, "get_managed_integration", lambda *_args, **_kwargs: integration)

    with pytest.raises(ManagedIntegrationActionError, match="cannot override"):
        await run_managed_integration_action(
            agent,
            "gamma-app",
            "list_themes",
            {"api_key_ref": "victim-key", "base_url": "https://attacker.invalid"},
            ["gamma-app"],
            lambda _ref: "x",
        )


@pytest.mark.asyncio
async def test_action_requires_globally_installed_and_agent_enabled_integration(monkeypatch) -> None:
    agent = SimpleNamespace(integration_configs={})
    monkeypatch.setattr(
        managed_integration_actions, "get_managed_integration", lambda *_args, **_kwargs: _integration()
    )
    with pytest.raises(ManagedIntegrationActionError, match="not enabled"):
        await run_managed_integration_action(agent, "gamma-app", "list_themes", {}, ["gamma-app"], lambda _ref: "x")

    monkeypatch.setattr(managed_integration_actions, "get_managed_integration", lambda *_args, **_kwargs: None)
    with pytest.raises(ManagedIntegrationActionError, match="not found"):
        await run_managed_integration_action(agent, "gamma-app", "list_themes", {}, [], lambda _ref: "x")


@pytest.mark.asyncio
async def test_healthcheck_uses_only_persisted_configuration(monkeypatch) -> None:
    integration = _integration()
    agent = SimpleNamespace(
        integration_configs={
            "gamma-app": {
                "api_key_ref": "agent-gamma-key",
                "base_url": "https://public-api.gamma.app/v1.0",
            }
        }
    )
    resolved_refs: list[str] = []
    captured: dict = {}

    async def resolve_secret(secret_ref: str) -> str:
        resolved_refs.append(secret_ref)
        return "configured-value"

    async def fake_healthcheck(*, config, resolve_secret):
        captured.update(config=config, secret=await resolve_secret(config["api_key_ref"]))
        return True, "ok", None

    monkeypatch.setattr(managed_integration_health, "get_managed_integration", lambda *_args, **_kwargs: integration)
    monkeypatch.setattr(
        managed_integration_health,
        "_load_healthcheck_module",
        lambda _integration: SimpleNamespace(test_connection=fake_healthcheck),
    )

    result = await run_managed_integration_test(agent, "gamma-app", ["gamma-app"], resolve_secret)

    assert result == (True, "ok", None)
    assert captured["config"]["api_key_ref"] == "agent-gamma-key"
    assert captured["config"]["base_url"] == "https://public-api.gamma.app/v1.0"
    assert captured["secret"] == "configured-value"
    assert resolved_refs == ["agent-gamma-key"]


@pytest.mark.asyncio
async def test_healthcheck_requires_agent_enabled_integration(monkeypatch) -> None:
    monkeypatch.setattr(managed_integration_health, "get_managed_integration", lambda *_args, **_kwargs: _integration())
    agent = SimpleNamespace(integration_configs={})
    with pytest.raises(ManagedIntegrationTestError, match="not enabled"):
        await run_managed_integration_test(agent, "gamma-app", ["gamma-app"], lambda _ref: "x")
