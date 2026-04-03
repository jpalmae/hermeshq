from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.message import AgentMessage
from hermeshq.models.task import Task
from hermeshq.schemas.message import BroadcastCreate, MessageCreate


class CommsRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker

    async def send_message(self, payload: MessageCreate) -> AgentMessage:
        async with self.session_factory() as session:
            dump = payload.model_dump()
            message = AgentMessage(
                from_agent_id=dump["from_agent_id"],
                to_agent_id=dump["to_agent_id"],
                message_type=dump["message_type"],
                content=dump["content"],
                metadata_json=dump["metadata"],
            )
            session.add(message)
            session.add(
                ActivityLog(
                    agent_id=payload.to_agent_id,
                    event_type="comms.message",
                    message=payload.content[:120],
                    details={"message_type": payload.message_type, "from_agent_id": payload.from_agent_id},
                )
            )
            if payload.message_type == "delegate":
                parent_task_id = None
                if isinstance(payload.metadata, dict):
                    raw_parent_task_id = payload.metadata.get("parent_task_id")
                    if isinstance(raw_parent_task_id, str) and raw_parent_task_id.strip():
                        parent_task_id = raw_parent_task_id.strip()
                delegated_task = Task(
                    agent_id=payload.to_agent_id,
                    source_agent_id=payload.from_agent_id,
                    parent_task_id=parent_task_id,
                    title=payload.metadata.get("title") if isinstance(payload.metadata, dict) else None,
                    prompt=payload.content,
                    metadata_json={"delegated": True, **payload.metadata},
                )
                session.add(delegated_task)
                await session.flush()
                message.task_id = delegated_task.id
            await session.commit()
            await session.refresh(message)

        await self.event_broker.publish(
            {
                "type": "comms.message",
                "message_id": message.id,
                "from_agent_id": message.from_agent_id,
                "to_agent_id": message.to_agent_id,
                "message_type": message.message_type,
                "content": message.content,
            }
        )
        return message

    async def broadcast(self, payload: BroadcastCreate) -> list[AgentMessage]:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent))
            recipients = [
                agent for agent in result.scalars().all() if payload.team_tag in (agent.team_tags or [])
            ]
        messages: list[AgentMessage] = []
        for recipient in recipients:
            message = await self.send_message(
                MessageCreate(
                    from_agent_id=payload.from_agent_id,
                    to_agent_id=recipient.id,
                    message_type="broadcast",
                    content=payload.content,
                    metadata={"team_tag": payload.team_tag, **payload.metadata},
                )
            )
            messages.append(message)
        return messages
