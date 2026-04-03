from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import can_access_agent, ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.message import AgentMessage
from hermeshq.models.user import User
from hermeshq.schemas.message import BroadcastCreate, MessageCreate, MessageRead

router = APIRouter(prefix="/comms", tags=["comms"])


@router.post("/send", response_model=MessageRead)
async def send_message(
    payload: MessageCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MessageRead:
    async with request.app.state.supervisor.session_factory() as session:
        await ensure_agent_access(session, current_user, payload.from_agent_id)
        if not await can_access_agent(session, current_user, payload.to_agent_id):
            raise HTTPException(status_code=403, detail="Destination agent access denied")
    message = await request.app.state.comms_router.send_message(payload)
    if payload.message_type == "delegate" and message.task_id:
        async with request.app.state.supervisor.session_factory() as session:
            agent = await session.get(Agent, payload.to_agent_id)
            should_start = bool(agent and agent.status != "running")
        if should_start:
            await request.app.state.supervisor.start_agent(payload.to_agent_id)
        await request.app.state.supervisor.submit_task(message.task_id)
    return MessageRead.model_validate(message)


@router.post("/broadcast", response_model=list[MessageRead])
async def broadcast(
    payload: BroadcastCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[MessageRead]:
    async with request.app.state.supervisor.session_factory() as session:
        await ensure_agent_access(session, current_user, payload.from_agent_id)
        if not is_admin(current_user):
            result = await session.execute(select(Agent))
            target_ids = {
                agent.id
                for agent in result.scalars().all()
                if payload.team_tag in (agent.team_tags or [])
            }
            accessible_ids = await get_accessible_agent_ids(session, current_user)
            if any(target_id not in accessible_ids for target_id in target_ids):
                raise HTTPException(status_code=403, detail="Broadcast target set includes agents outside this user's scope")
    messages = await request.app.state.comms_router.broadcast(payload)
    return [MessageRead.model_validate(item) for item in messages]


@router.get("/history", response_model=list[MessageRead])
async def history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageRead]:
    statement = select(AgentMessage).order_by(desc(AgentMessage.created_at)).limit(200)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = (
            statement.where(or_(AgentMessage.from_agent_id.in_(accessible_ids), AgentMessage.to_agent_id.in_(accessible_ids)))
            if accessible_ids
            else statement.where(false())
        )
    result = await db.execute(statement)
    return [MessageRead.model_validate(item) for item in result.scalars().all()]


@router.get("/topology")
async def topology(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agents_statement = select(Agent).order_by(Agent.created_at.asc())
    messages_statement = select(AgentMessage).order_by(desc(AgentMessage.created_at)).limit(300)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        agents_statement = agents_statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else agents_statement.where(false())
        messages_statement = (
            messages_statement.where(or_(AgentMessage.from_agent_id.in_(accessible_ids), AgentMessage.to_agent_id.in_(accessible_ids)))
            if accessible_ids
            else messages_statement.where(false())
        )
    agents_result = await db.execute(agents_statement)
    messages_result = await db.execute(messages_statement)
    agents = agents_result.scalars().all()
    messages = messages_result.scalars().all()
    return {
        "nodes": [
            {"id": agent.id, "label": agent.friendly_name or agent.name, "slug": agent.slug, "status": agent.status}
            for agent in agents
        ],
        "edges": [
            {
                "id": message.id,
                "source": message.from_agent_id,
                "target": message.to_agent_id,
                "type": message.message_type,
            }
            for message in messages
        ],
    }
