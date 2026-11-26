from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventAudience, EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.message import AgentMessage
from hermeshq.models.task import Task
from hermeshq.schemas.message import BroadcastCreate, MessageCreate
from hermeshq.services.metadata_delegacion import metadata_delegacion_segura


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
            es_delegacion = payload.message_type == "delegate"
            metadata = metadata_delegacion_segura(dump["metadata"]) if es_delegacion else dump["metadata"]
            message = AgentMessage(
                from_agent_id=dump["from_agent_id"],
                to_agent_id=dump["to_agent_id"],
                message_type=dump["message_type"],
                content=dump["content"],
                metadata_json=metadata,
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
            if es_delegacion:
                delegated_task = Task(
                    agent_id=payload.to_agent_id,
                    source_agent_id=payload.from_agent_id,
                    parent_task_id=metadata.get("parent_task_id"),
                    title=metadata.get("title"),
                    prompt=payload.content,
                    metadata_json={**metadata, "delegated": True},
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
            },
            audience=EventAudience.for_agents(message.from_agent_id, message.to_agent_id),
        )
        return message

    MAX_BROADCAST_RECIPIENTS = 100

    async def broadcast(self, payload: BroadcastCreate) -> list[AgentMessage]:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent))
            recipients = [agent for agent in result.scalars().all() if payload.team_tag in (agent.team_tags or [])]
        if len(recipients) > self.MAX_BROADCAST_RECIPIENTS:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail=f"Broadcast target group has {len(recipients)} agents; limit is {self.MAX_BROADCAST_RECIPIENTS}",
            )
        messages: list[AgentMessage] = []
        async with self.session_factory() as session:
            for recipient in recipients:
                msg = AgentMessage(
                    from_agent_id=payload.from_agent_id,
                    to_agent_id=recipient.id,
                    message_type="broadcast",
                    content=payload.content,
                    metadata_json={"team_tag": payload.team_tag, **payload.metadata},
                )
                session.add(msg)
                session.add(
                    ActivityLog(
                        agent_id=recipient.id,
                        event_type="comms.message",
                        message=payload.content[:120],
                        details={"message_type": "broadcast", "from_agent_id": payload.from_agent_id},
                    )
                )
                messages.append(msg)
            await session.commit()
            for msg in messages:
                await session.refresh(msg)
        for msg in messages:
            await self.event_broker.publish(
                {
                    "type": "comms.message",
                    "message_id": msg.id,
                    "from_agent_id": msg.from_agent_id,
                    "to_agent_id": msg.to_agent_id,
                    "message_type": msg.message_type,
                    "content": msg.content,
                },
                audience=EventAudience.for_agents(msg.from_agent_id, msg.to_agent_id),
            )
        return messages
