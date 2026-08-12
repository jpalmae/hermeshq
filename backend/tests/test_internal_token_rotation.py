from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from hermeshq.routers.agents_runtime import rotate_agent_service_token
from hermeshq.routers.internal_control import control_run_integration_action


@pytest.mark.asyncio
async def test_integration_action_rejects_cross_agent_credentials() -> None:
    request = SimpleNamespace(
        headers={
            "X-HermesHQ-Agent-ID": "agent-a",
            "X-HermesHQ-Agent-Token": "token",
        }
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await control_run_integration_action(
            agent_id="agent-b",
            integration_slug="example",
            action_slug="run",
            payload={},
            request=request,
            db=db,
        )
    assert exc_info.value.status_code == 403
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_can_revoke_agent_tokens_by_rotating_version() -> None:
    agent = SimpleNamespace(
        id="agent-1",
        name="Agent One",
        is_archived=False,
        status="stopped",
        service_token_version=1,
    )
    result = MagicMock()
    result.scalars.return_value = []
    db = AsyncMock()
    db.get.return_value = agent
    db.execute.return_value = result
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )
    admin = SimpleNamespace(id="admin-1", username="admin", role="admin")

    with patch("hermeshq.routers.agents_runtime.record_audit", AsyncMock()) as audit:
        response = await rotate_agent_service_token("agent-1", request, admin, db)

    assert response["token_version"] == 2
    assert agent.service_token_version == 2
    db.commit.assert_awaited_once()
    audit.assert_awaited_once()
