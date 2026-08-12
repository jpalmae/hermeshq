"""Tests for the Nous Research provider integration."""

from unittest.mock import MagicMock

import pytest

from hermeshq.models.agent import Agent
from hermeshq.services.hermes_version_manager import HermesRuntimeSelection
from hermeshq.services.provider_catalog import (
    BUILTIN_PROVIDERS,
    normalize_runtime_provider,
    seed_provider_defaults,
)

# ── Helpers ──────────────────────────────────────────────────


def _find_provider(slug: str) -> dict | None:
    return next((p for p in BUILTIN_PROVIDERS if p["slug"] == slug), None)


def _make_mock_provider(slug: str = "nous-api") -> MagicMock:
    """Build a lightweight mock that mimics ProviderDefinition without SQLAlchemy deps."""
    payload = _find_provider(slug)
    assert payload, f"Provider {slug!r} not in BUILTIN_PROVIDERS"
    mock = MagicMock()
    for key, value in payload.items():
        setattr(mock, key, value)
    return mock


# ── Catalog entry tests ──────────────────────────────────────


class TestNousProviderCatalogEntry:
    """The nous-api provider must be correctly defined in the catalog."""

    def test_entry_exists(self):
        entry = _find_provider("nous-api")
        assert entry is not None

    def test_runtime_provider_is_openai_codex(self):
        entry = _find_provider("nous-api")
        assert entry["runtime_provider"] == "openai-codex"

    def test_auth_type_is_api_key(self):
        entry = _find_provider("nous-api")
        assert entry["auth_type"] == "api_key"

    def test_base_url_points_to_inference_api(self):
        entry = _find_provider("nous-api")
        assert entry["base_url"] == "https://inference-api.nousresearch.com/v1"

    def test_default_model_is_stepfun_free(self):
        entry = _find_provider("nous-api")
        assert entry["default_model"] == "stepfun/step-3.7-flash:free"

    def test_available_models_contains_key_models(self):
        entry = _find_provider("nous-api")
        models = entry["available_models"]
        assert "stepfun/step-3.7-flash:free" in models
        assert "nousresearch/hermes-4-70b" in models

    def test_supports_secret_ref(self):
        entry = _find_provider("nous-api")
        assert entry["supports_secret_ref"] is True

    def test_supports_custom_base_url(self):
        entry = _find_provider("nous-api")
        assert entry["supports_custom_base_url"] is True

    def test_enabled_by_default(self):
        entry = _find_provider("nous-api")
        assert entry["enabled"] is True

    def test_sort_order_between_kimi_and_zai(self):
        entry = _find_provider("nous-api")
        kimi = _find_provider("kimi-coding")
        zai = _find_provider("zai")
        assert kimi["sort_order"] < entry["sort_order"] < zai["sort_order"]

    def test_secret_placeholder(self):
        entry = _find_provider("nous-api")
        assert entry["secret_placeholder"] == "Nous API key"

    def test_description_mentions_free_tier(self):
        entry = _find_provider("nous-api")
        assert "free" in entry["description"].lower()

    def test_docs_url(self):
        entry = _find_provider("nous-api")
        assert entry["docs_url"] == "https://portal.nousresearch.com/api-docs"


class TestNvidiaProviderCatalogEntry:
    def test_deepseek_v4_flash_uses_live_model_id(self):
        entry = _find_provider("nvidia-nim")
        assert "deepseek-ai/deepseek-v4-flash-0731" in entry["available_models"]
        assert "deepseek-ai/deepseek-v4-flash" not in entry["available_models"]


# ── Catalog dict structure tests ─────────────────────────────


class TestNousProviderCatalogDict:
    """Validate the raw catalog dict (no ORM instantiation needed)."""

    def test_slug_is_nous_api(self):
        entry = _find_provider("nous-api")
        assert entry["slug"] == "nous-api"

    def test_name(self):
        entry = _find_provider("nous-api")
        assert entry["name"] == "Nous Research API"

    def test_available_models_is_list_with_min_5(self):
        entry = _find_provider("nous-api")
        assert isinstance(entry["available_models"], list)
        assert len(entry["available_models"]) >= 5

    def test_normalize_nous_runtime_provider(self):
        """normalize_runtime_provider should NOT alias 'nous'."""
        result = normalize_runtime_provider("nous")
        assert result == "nous"

    def test_normalize_unknown_still_passthrough(self):
        result = normalize_runtime_provider("some-future-provider")
        assert result == "some-future-provider"

    def test_all_required_keys_present(self):
        required = [
            "slug",
            "name",
            "runtime_provider",
            "auth_type",
            "base_url",
            "default_model",
            "available_models",
            "description",
            "docs_url",
            "secret_placeholder",
            "supports_secret_ref",
            "supports_custom_base_url",
            "enabled",
            "sort_order",
        ]
        entry = _find_provider("nous-api")
        for key in required:
            assert key in entry, f"Missing key: {key}"

    def test_no_extra_keys(self):
        entry = _find_provider("nous-api")
        expected = {
            "slug",
            "name",
            "runtime_provider",
            "auth_type",
            "base_url",
            "default_model",
            "available_models",
            "description",
            "docs_url",
            "secret_placeholder",
            "supports_secret_ref",
            "supports_custom_base_url",
            "enabled",
            "sort_order",
        }
        extra = set(entry.keys()) - expected
        assert not extra, f"Unexpected keys: {extra}"


# ── Seed defaults tests ──────────────────────────────────────


class TestNousProviderSeedDefaults:
    """seed_provider_defaults must fill in missing fields on an existing provider."""

    def test_seed_fills_empty_base_url(self):
        provider = _make_mock_provider()
        provider.base_url = None
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.base_url == "https://inference-api.nousresearch.com/v1"

    def test_seed_fills_empty_default_model(self):
        provider = _make_mock_provider()
        provider.default_model = None
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.default_model == "stepfun/step-3.7-flash:free"

    def test_seed_does_not_overwrite_existing_base_url(self):
        provider = _make_mock_provider()
        provider.base_url = "https://custom-proxy.example.com/v1"
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.base_url == "https://custom-proxy.example.com/v1"

    def test_seed_does_not_overwrite_existing_default_model(self):
        provider = _make_mock_provider()
        provider.default_model = "nousresearch/hermes-4-405b"
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.default_model == "nousresearch/hermes-4-405b"

    def test_seed_fills_empty_available_models(self):
        provider = _make_mock_provider()
        provider.available_models = []
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert "stepfun/step-3.7-flash:free" in provider.available_models

    def test_seed_does_not_overwrite_existing_available_models(self):
        provider = _make_mock_provider()
        provider.available_models = ["custom-model-a"]
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.available_models == ["custom-model-a"]

    def test_seed_always_sets_runtime_provider(self):
        """Even if mutated, seed resets runtime_provider from catalog."""
        provider = _make_mock_provider()
        provider.runtime_provider = "wrong"
        payload = _find_provider("nous-api")
        seed_provider_defaults(provider, payload)
        assert provider.runtime_provider == "openai-codex"

    def test_seed_returns_none_on_none_existing(self):
        payload = _find_provider("nous-api")
        result = seed_provider_defaults(None, payload)
        assert result is None


# ── Hermes installation fallback tests ───────────────────────


class TestNousProviderEnvFallbacks:
    """Verify that hermes_installation fallback dicts include 'nous'."""

    def test_nous_in_env_names_fallback(self):
        """The _provider_env_names fallback dict must include nous."""
        # We can't easily call _provider_env_names without a full instance,
        # so we verify the fallback dict directly by inspecting the source.
        import inspect

        from hermeshq.services.hermes_installation import HermesInstallationManager

        source = inspect.getsource(HermesInstallationManager._provider_env_names)
        assert '"nous"' in source or "'nous'" in source

    def test_nous_env_var_is_noous_api_key(self):
        """The env var for nous should be NOUS_API_KEY."""
        import inspect

        from hermeshq.services.hermes_installation import HermesInstallationManager

        source = inspect.getsource(HermesInstallationManager._provider_env_names)
        assert "NOUS_API_KEY" in source

    def test_nous_in_base_url_fallback(self):
        """The _provider_base_url_env_name fallback dict must include nous."""
        import inspect

        from hermeshq.services.hermes_installation import HermesInstallationManager

        source = inspect.getsource(HermesInstallationManager._provider_base_url_env_name)
        assert '"nous"' in source or "'nous'" in source

    def test_nous_base_url_env_var(self):
        """The base_url env var for nous should be NOUS_BASE_URL."""
        import inspect

        from hermeshq.services.hermes_installation import HermesInstallationManager

        source = inspect.getsource(HermesInstallationManager._provider_base_url_env_name)
        assert "NOUS_BASE_URL" in source

    @pytest.mark.asyncio
    async def test_build_process_env_sets_provider_api_key(self, monkeypatch):
        """build_process_env should use provider env names when an API key is resolved."""
        from hermeshq.services.hermes_installation import HermesInstallationManager

        manager = HermesInstallationManager(session_factory=None, secret_vault=None, version_manager=None)
        agent = Agent(
            id="agent-1",
            node_id="node-1",
            name="Nous Agent",
            slug="nous-agent",
            runtime_profile="technical",
            provider="nous",
            api_key_ref="nous-secret",
            workspace_path="/tmp/hermeshq-test-agent",
        )

        async def resolve_runtime(_agent):
            return HermesRuntimeSelection(
                requested_version=None,
                effective_version="test",
                source="bundled",
                python_bin="python",
                hermes_bin="hermes",
                detected_version=None,
            )

        async def resolve_api_key(_ref):
            return "secret-value"

        async def build_managed_env_map(_agent):
            return {}

        monkeypatch.setattr(manager, "resolve_hermes_runtime", resolve_runtime)
        monkeypatch.setattr(manager, "_resolve_api_key", resolve_api_key)
        monkeypatch.setattr(manager, "_build_managed_env_map", build_managed_env_map)

        env = await manager.build_process_env(agent)

        assert env["NOUS_API_KEY"] == "secret-value"
