from types import SimpleNamespace

from hermeshq.routers.agents_crud import _runtime_profile_changed


class TestRuntimeProfileResave:
    def test_resending_same_profile_is_not_a_change(self):
        agent = SimpleNamespace(runtime_profile="standard")
        assert _runtime_profile_changed(agent, {"runtime_profile": "standard", "name": "x"}) is False

    def test_missing_field_is_not_a_change(self):
        agent = SimpleNamespace(runtime_profile="standard")
        assert _runtime_profile_changed(agent, {"name": "x"}) is False

    def test_different_profile_is_a_change(self):
        agent = SimpleNamespace(runtime_profile="standard")
        assert _runtime_profile_changed(agent, {"runtime_profile": "technical"}) is True

    def test_legacy_value_normalizes_to_standard(self):
        agent = SimpleNamespace(runtime_profile=None)
        assert _runtime_profile_changed(agent, {"runtime_profile": "standard"}) is False


class TestCronjobToolsetNotStandard:
    def test_native_cronjob_is_not_a_standard_toolset(self):
        from hermeshq.services.runtime_profiles import STANDARD_ENABLED_TOOLSETS

        assert "cronjob" not in STANDARD_ENABLED_TOOLSETS
