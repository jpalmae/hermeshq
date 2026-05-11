from datetime import datetime

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel
from hermeshq.schemas.node import NodeRead


class AgentCreate(BaseModel):
    node_id: str
    name: str = ""
    friendly_name: str | None = None
    slug: str = ""
    description: str | None = None
    run_mode: str = "hybrid"
    runtime_profile: str = "standard"
    hermes_version: str | None = None
    approval_mode: str | None = None
    tool_progress_mode: str | None = None
    gateway_notifications_mode: str | None = None
    model: str | None = None
    provider: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    soul_md: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    skills: list[str] = []
    integration_configs: dict[str, dict] | None = None
    team_tags: list[str] = []
    supervisor_agent_id: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    friendly_name: str | None = None
    slug: str | None = None
    description: str | None = None
    run_mode: str | None = None
    runtime_profile: str | None = None
    hermes_version: str | None = None
    approval_mode: str | None = None
    tool_progress_mode: str | None = None
    gateway_notifications_mode: str | None = None
    model: str | None = None
    provider: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    soul_md: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    skills: list[str] | None = None
    integration_configs: dict[str, dict] | None = None
    team_tags: list[str] | None = None
    status: str | None = None
    supervisor_agent_id: str | None = None


class AgentRead(ORMModel):
    id: str
    node_id: str
    name: str
    friendly_name: str | None
    slug: str
    avatar_url: str | None = None
    has_avatar: bool = False
    description: str | None
    status: str
    run_mode: str
    runtime_profile: str
    hermes_version: str | None
    approval_mode: str | None
    tool_progress_mode: str | None
    gateway_notifications_mode: str | None
    model: str
    provider: str
    api_key_ref: str | None
    base_url: str | None
    system_prompt: str | None
    workspace_path: str
    enabled_toolsets: list[str]
    disabled_toolsets: list[str]
    skills: list[str]
    integration_configs: dict[str, dict]
    team_tags: list[str]
    is_system_agent: bool
    system_scope: str | None
    can_receive_tasks: bool
    can_send_tasks: bool
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    supervisor_agent_id: str | None
    total_tasks: int
    total_tokens_used: int
    last_activity: datetime | None
    created_at: datetime
    updated_at: datetime
    node: NodeRead | None = None


class AgentBulkTaskCreate(BaseModel):
    agent_ids: list[str] = Field(default_factory=list)
    title: str
    prompt: str
    priority: int = 5
    auto_start_stopped: bool = False


class AgentBulkMessageCreate(BaseModel):
    agent_ids: list[str] = Field(default_factory=list)
    message: str
    auto_start_stopped: bool = False


class AgentBulkOperationSkipped(BaseModel):
    agent_id: str
    reason: str


class AgentBulkOperationResult(ORMModel):
    batch_id: str | None = None
    submitted: int
    skipped: int
    submitted_agent_ids: list[str]
    skipped_agents: list[AgentBulkOperationSkipped]
    task_ids: list[str] = Field(default_factory=list)


class AgentModeUpdate(BaseModel):
    """Payload for setting an agent's run mode."""

    mode: str = Field(
        ...,
        description="The run mode for the agent. Must be one of: headless, interactive, hybrid",
    )


class AgentTemplateOverrides(BaseModel):
    """Optional overrides applied when creating an agent from a template.

    All fields default to ``None`` so the body can be empty (``{}``) and the
    template config is used as-is.  Any provided values override the
    corresponding template fields.
    """

    node_id: str | None = None
    name: str | None = None
    friendly_name: str | None = None
    slug: str | None = None
    description: str | None = None
    run_mode: str | None = None
    runtime_profile: str | None = None
    hermes_version: str | None = None
    approval_mode: str | None = None
    tool_progress_mode: str | None = None
    gateway_notifications_mode: str | None = None
    model: str | None = None
    provider: str | None = None
    api_key_ref: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    soul_md: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    skills: list[str] | None = None
    integration_configs: dict[str, dict] | None = None
    team_tags: list[str] | None = None
    supervisor_agent_id: str | None = None


class WorkspaceFileWrite(BaseModel):
    """Payload for writing content to an agent workspace file."""

    content: str = Field(
        default="",
        description="The file content to write",
    )
