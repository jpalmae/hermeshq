from fastapi import APIRouter, Depends, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
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
    _: User = Depends(get_current_user),
) -> MessageRead:
    message = await request.app.state.comms_router.send_message(payload)
    if payload.message_type == "delegate" and message.task_id:
        await request.app.state.supervisor.submit_task(message.task_id)
    return MessageRead.model_validate(message)


@router.post("/broadcast", response_model=list[MessageRead])
async def broadcast(
    payload: BroadcastCreate,
    request: Request,
    _: User = Depends(get_current_user),
) -> list[MessageRead]:
    messages = await request.app.state.comms_router.broadcast(payload)
    return [MessageRead.model_validate(item) for item in messages]


@router.get("/history", response_model=list[MessageRead])
async def history(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageRead]:
    result = await db.execute(select(AgentMessage).order_by(desc(AgentMessage.created_at)).limit(200))
    return [MessageRead.model_validate(item) for item in result.scalars().all()]


@router.get("/topology")
async def topology(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agents_result = await db.execute(select(Agent).order_by(Agent.created_at.asc()))
    messages_result = await db.execute(select(AgentMessage).order_by(desc(AgentMessage.created_at)).limit(300))
    agents = agents_result.scalars().all()
    messages = messages_result.scalars().all()
    return {
        "nodes": [
            {"id": agent.id, "label": agent.name, "slug": agent.slug, "status": agent.status}
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

