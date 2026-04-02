from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel
from hermeshq.schemas.node import NodeRead


class AgentCreate(BaseModel):
    node_id: str
    name: str = ""
    friendly_name: str | None = None
    slug: str = ""
    description: str | None = None
    run_mode: str = "hybrid"
    model: str | None = None
    provider: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    soul_md: str | None = None
    enabled_toolsets: list[str] = []
    disabled_toolsets: list[str] = []
    skills: list[str] = []
    team_tags: list[str] = []
    supervisor_agent_id: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    friendly_name: str | None = None
    slug: str | None = None
    description: str | None = None
    run_mode: str | None = None
    model: str | None = None
    provider: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    soul_md: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    skills: list[str] | None = None
    team_tags: list[str] | None = None
    status: str | None = None
    supervisor_agent_id: str | None = None


class AgentRead(ORMModel):
    id: str
    node_id: str
    name: str
    friendly_name: str | None
    slug: str
    description: str | None
    status: str
    run_mode: str
    model: str
    provider: str
    api_key_ref: str | None
    base_url: str | None
    system_prompt: str | None
    workspace_path: str
    enabled_toolsets: list[str]
    disabled_toolsets: list[str]
    skills: list[str]
    team_tags: list[str]
    supervisor_agent_id: str | None
    total_tasks: int
    total_tokens_used: int
    last_activity: datetime | None
    created_at: datetime
    updated_at: datetime
    node: NodeRead | None = None
