from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class RuntimeLedgerEntryRead(BaseModel):
    id: str
    agent_id: str
    channel: str
    direction: str
    entry_type: str
    title: str | None
    content: str | None
    status: str | None
    task_id: str | None
    message_id: str | None
    counterpart_agent_id: str | None
    counterpart_label: str | None
    details: dict
    created_at: datetime


class RuntimeLedgerResponse(ORMModel):
    entries: list[RuntimeLedgerEntryRead]
