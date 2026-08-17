import pytest
from conftest import requires_database

from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.routers.dashboard import fleet_health

pytestmark = [pytest.mark.integration, requires_database]


async def test_fleet_health_scopes_recent_errors_to_accessible_agents(db_session) -> None:
    node = Node(name="dashboard-node", hostname="dashboard-node")
    user = User(
        username="dashboard-user",
        display_name="Dashboard User",
        password_hash="unused",
        role="user",
    )
    admin = User(
        username="dashboard-admin",
        display_name="Dashboard Admin",
        password_hash="unused",
        role="admin",
    )
    db_session.add_all([node, user, admin])
    await db_session.flush()

    allowed_agent = Agent(
        node_id=node.id,
        name="Allowed Agent",
        slug="dashboard-allowed",
        workspace_path="/tmp/dashboard-allowed",
    )
    private_agent = Agent(
        node_id=node.id,
        name="Private Agent",
        slug="dashboard-private",
        workspace_path="/tmp/dashboard-private",
    )
    db_session.add_all([allowed_agent, private_agent])
    await db_session.flush()
    db_session.add(AgentAssignment(user_id=user.id, agent_id=allowed_agent.id))
    db_session.add_all(
        [
            ActivityLog(
                agent_id=allowed_agent.id,
                event_type="task.failed",
                severity="error",
                message="allowed error",
            ),
            ActivityLog(
                agent_id=private_agent.id,
                event_type="task.failed",
                severity="error",
                message="private error",
            ),
            ActivityLog(
                event_type="system.failed",
                severity="error",
                message="global error",
            ),
        ]
    )
    await db_session.commit()

    user_result = await fleet_health(current_user=user, db=db_session)
    admin_result = await fleet_health(current_user=admin, db=db_session)

    assert {item["message"] for item in user_result["recent_errors"]} == {"allowed error"}
    assert {item["message"] for item in admin_result["recent_errors"]} == {
        "allowed error",
        "private error",
        "global error",
    }
