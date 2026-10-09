"""Regression tests for M365 cross-user token theft (S2 vulnerability chain).

Three attack steps were patched:
  Step 1 — GET /m365/me/agents/{id}/scopes used to auto-create an AgentAssignment
            for any agent_id without authorization check.
            Fixed: ensure_agent_access() raises 403 for non-assigned users.

  Step 2 — POST /api/tasks preserved a client-supplied thread_user_id in metadata,
            allowing an attacker to inject another user's identity into the task.
            Fixed: thread_user_id is always overwritten by the authenticated user's id.

  Step 3 — PUT /me/agents/{id}/scopes used to allow non-admin users to mutate
            shared agent properties (skills, toolsets, integration_configs).
            Fixed: agent mutation is gated behind is_admin().
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(user_id: str = "user-A", role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, role=role, is_active=True)


def _make_admin(user_id: str = "admin-1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, role="admin", is_active=True)


def _make_db(scalar=None):
    """Minimal async DB session mock."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = scalar
    result_mock.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.add = MagicMock()
    db.execute.return_value = result_mock
    db.get.return_value = None
    return db


# ---------------------------------------------------------------------------
# Step 2 regression: thread_user_id is pinned to the authenticated user
# ---------------------------------------------------------------------------


class TestTaskCreateIdentityPinning(unittest.IsolatedAsyncioTestCase):
    """POST /api/tasks must not honour client-supplied identity fields."""

    async def _call_create_task(self, requesting_user, payload_metadata: dict) -> dict:
        """Invoke the create_task handler directly with a mocked environment."""
        from hermeshq.schemas.task import TaskCreate

        # Agent that is running and not archived
        agent = SimpleNamespace(
            id="agent-1",
            is_archived=False,
            status="running",
        )
        db = _make_db()
        db.get.return_value = agent

        captured_metadata: dict = {}

        def _capture_task(**kwargs):
            captured_metadata.update(kwargs.get("metadata_json", {}))
            task = SimpleNamespace(
                id="task-1",
                agent_id="agent-1",
                status="queued",
                board_column="queue",
                board_order=1,
                board_manual=False,
                metadata_json=captured_metadata,
                created_by_user_id=requesting_user.id,
                queued_at=None,
                title=None,
                prompt="test",
            )
            return task

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor=AsyncMock())))

        payload = TaskCreate(
            agent_id="agent-1",
            prompt="list my files",
            metadata=payload_metadata,
        )

        with (
            patch("hermeshq.routers.tasks.ensure_agent_access", AsyncMock(return_value=agent)),
            patch("hermeshq.routers.tasks.Task", side_effect=_capture_task),
            patch("hermeshq.routers.tasks.runtime_status_to_board_column", return_value="queue"),
            patch("hermeshq.routers.tasks.next_board_order", return_value=1),
            patch("hermeshq.routers.tasks.TaskRead.model_validate", lambda t: t),
        ):
            from hermeshq.routers.tasks import create_task

            await create_task(
                payload=payload,
                request=request,
                current_user=requesting_user,
                db=db,
            )

        return captured_metadata

    async def test_thread_user_id_is_always_the_authenticated_user(self) -> None:
        """Even if the client sends thread_user_id=victim, it must be overridden."""
        user = _make_user("user-A")
        metadata = await self._call_create_task(
            requesting_user=user,
            payload_metadata={"thread_user_id": "victim-user"},
        )
        self.assertEqual(
            metadata["thread_user_id"],
            "user-A",
            "thread_user_id must be pinned to the authenticated user, not the client-supplied value",
        )

    async def test_created_by_user_id_stripped_from_metadata(self) -> None:
        """created_by_user_id must not appear in the metadata (task field is authoritative)."""
        user = _make_user("user-A")
        metadata = await self._call_create_task(
            requesting_user=user,
            payload_metadata={"created_by_user_id": "victim-user", "thread_user_id": "victim-user"},
        )
        self.assertNotIn(
            "created_by_user_id",
            metadata,
            "created_by_user_id must be stripped from client-supplied metadata",
        )

    async def test_legitimate_metadata_fields_preserved(self) -> None:
        """Non-identity metadata fields must pass through unchanged."""
        user = _make_user("user-A")
        metadata = await self._call_create_task(
            requesting_user=user,
            payload_metadata={"conversation": True, "source": "chat"},
        )
        self.assertTrue(metadata.get("conversation"))
        self.assertEqual(metadata.get("source"), "chat")
        self.assertEqual(metadata["thread_user_id"], "user-A")


# ---------------------------------------------------------------------------
# Step 1 regression: GET /scopes requires existing assignment
# ---------------------------------------------------------------------------


class TestGetAgentScopesRequiresAssignment(unittest.IsolatedAsyncioTestCase):
    """GET /m365/me/agents/{id}/scopes must not auto-create an AgentAssignment."""

    async def test_unassigned_user_gets_403(self) -> None:
        """A user without an assignment must receive 403, not an auto-created row."""
        from fastapi import HTTPException

        user = _make_user("user-A")
        db = _make_db(scalar=None)  # no assignment found

        with patch("hermeshq.core.security.ensure_agent_access", AsyncMock(side_effect=HTTPException(status_code=403))):
            from hermeshq.routers.m365 import get_agent_m365_scopes

            with self.assertRaises(HTTPException) as ctx:
                await get_agent_m365_scopes(
                    agent_id="agent-1",
                    current_user=user,
                    db=db,
                )
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_db_execute_not_called_before_authz(self) -> None:
        """The assignment lookup must not run before ensure_agent_access passes."""
        from fastapi import HTTPException

        user = _make_user("user-A")
        db = _make_db(scalar=None)

        with patch("hermeshq.core.security.ensure_agent_access", AsyncMock(side_effect=HTTPException(status_code=403))):
            from hermeshq.routers.m365 import get_agent_m365_scopes

            try:
                await get_agent_m365_scopes(agent_id="agent-1", current_user=user, db=db)
            except Exception:
                pass
            # DB must not have been queried for assignment before authz check
            db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Step 3 regression: non-admin cannot mutate shared agent properties
# ---------------------------------------------------------------------------


class TestPutScopesAgentMutationGating(unittest.IsolatedAsyncioTestCase):
    """PUT /m365/me/agents/{id}/scopes must gate agent mutations behind is_admin."""

    def _make_assignment(self, user_id="user-A", agent_id="agent-1"):
        a = MagicMock()
        a.user_id = user_id
        a.agent_id = agent_id
        a.m365_allowed_scopes = None
        a.sharepoint_site_url = None
        return a

    async def test_non_admin_cannot_mutate_agent_toolsets(self) -> None:
        """A regular user saving scopes must not touch agent.skills or enabled_toolsets."""
        from hermeshq.routers.m365 import AgentScopesUpdate, update_agent_m365_scopes

        user = _make_user("user-A", role="user")
        assignment = self._make_assignment()
        agent = MagicMock()
        agent.integration_configs = {}
        agent.skills = []
        agent.enabled_toolsets = []

        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=assignment))
        db.get.return_value = agent
        db.commit = AsyncMock()

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(installation_manager=AsyncMock())))

        payload = AgentScopesUpdate(
            allowed_scopes=["Files.Read.All"],
            sharepoint_site_url="https://contoso.sharepoint.com/sites/Test",
        )

        with patch("hermeshq.routers.m365.is_admin", return_value=False):
            await update_agent_m365_scopes(
                agent_id="agent-1",
                payload=payload,
                request=request,
                current_user=user,
                db=db,
            )

        # Agent shared properties must be untouched
        self.assertEqual(agent.skills, [])
        self.assertEqual(agent.enabled_toolsets, [])
        self.assertEqual(agent.integration_configs, {})

        # But the assignment must be updated
        self.assertEqual(assignment.m365_allowed_scopes, ["Files.Read.All"])
        self.assertEqual(assignment.sharepoint_site_url, "https://contoso.sharepoint.com/sites/Test")

    async def test_admin_can_mutate_agent_toolsets(self) -> None:
        """An admin saving scopes may add integrations to the shared agent."""
        from hermeshq.routers.m365 import AgentScopesUpdate, update_agent_m365_scopes

        admin = _make_admin("admin-1")
        assignment = self._make_assignment(user_id="admin-1")
        agent = MagicMock()
        agent.integration_configs = {}
        agent.skills = []
        agent.enabled_toolsets = []
        agent.id = "agent-1"

        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=assignment))
        db.get.return_value = agent
        db.commit = AsyncMock()

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(installation_manager=AsyncMock())))

        payload = AgentScopesUpdate(allowed_scopes=["Files.Read.All"])

        with patch("hermeshq.routers.m365.is_admin", return_value=True):
            await update_agent_m365_scopes(
                agent_id="agent-1",
                payload=payload,
                request=request,
                current_user=admin,
                db=db,
            )

        # Admin triggers integration enabling on the shared agent
        # integration_configs["sharepoint"] should have been added
        self.assertIn("sharepoint", agent.integration_configs)


if __name__ == "__main__":
    unittest.main()
