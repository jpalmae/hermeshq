from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class NodeCreate(BaseModel):
    name: str
    hostname: str
    node_type: str = "local"
    ssh_user: str | None = None
    ssh_port: int = 22
    max_agents: int = 10


class NodeUpdate(BaseModel):
    name: str | None = None
    hostname: str | None = None
    node_type: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    max_agents: int | None = None
    status: str | None = None


class NodeRead(ORMModel):
    id: str
    name: str
    hostname: str
    node_type: str
    status: str
    ssh_user: str | None
    ssh_port: int
    max_agents: int
    last_heartbeat: datetime | None
    system_info: dict
    created_at: datetime
    updated_at: datetime

