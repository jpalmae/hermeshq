import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.routers import agents_bulk
from hermeshq.schemas.agent import AgentBulkConfigUpdate


def _agent(**overrides):
    base = {
        "id": "a1",
        "is_archived": False,
        "runtime_profile": "standard",
        "integration_configs": {},
        "enabled_toolsets": ["safe"],
        "disabled_toolsets": ["mi_toolset_x"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _request():
    installation_manager = SimpleNamespace(sync_agent_installation=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(installation_manager=installation_manager))
    return SimpleNamespace(client=None, app=app)


def _admin():
    return SimpleNamespace(role="admin", id="u1", username="admin")


class BulkConfigToolsetTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, agent, payload):
        db = MagicMock()
        db.commit = AsyncMock()
        with (
            patch.object(agents_bulk, "_load_bulk_agents", AsyncMock(return_value=[agent])),
            patch.object(agents_bulk, "_load_enabled_integration_slugs", AsyncMock(return_value=[])),
            patch.object(agents_bulk, "_sync_agent_integration_toolsets", lambda *a, **k: None),
            patch.object(agents_bulk, "record_audit", AsyncMock()),
        ):
            await agents_bulk.bulk_config_update(payload, _request(), _admin(), db)

    async def test_resending_same_profile_keeps_disabled_toolsets(self):
        agent = _agent()
        payload = AgentBulkConfigUpdate(agent_ids=["a1"], runtime_profile="standard", system_prompt="Hola")
        await self._run(agent, payload)
        self.assertEqual(agent.disabled_toolsets, ["mi_toolset_x"])

    async def test_update_without_profile_keeps_disabled_toolsets(self):
        agent = _agent()
        payload = AgentBulkConfigUpdate(agent_ids=["a1"], system_prompt="Hola")
        await self._run(agent, payload)
        self.assertEqual(agent.disabled_toolsets, ["mi_toolset_x"])

    async def test_actual_profile_change_still_applies_profile_defaults(self):
        agent = _agent()
        payload = AgentBulkConfigUpdate(agent_ids=["a1"], runtime_profile="technical")
        await self._run(agent, payload)
        self.assertNotIn("mi_toolset_x", agent.disabled_toolsets)


if __name__ == "__main__":
    unittest.main()
