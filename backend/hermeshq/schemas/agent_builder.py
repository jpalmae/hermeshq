"""Schemas for the AI Agent Builder."""

from pydantic import BaseModel, Field


class AgentBuilderMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class RequiredConnector(BaseModel):
    slug: str
    name: str
    installed: bool
    required_fields: list[str] = []
    admin_instructions: str


class AgentDraft(BaseModel):
    name: str | None = None
    friendly_name: str | None = None
    slug: str | None = None
    description: str | None = None
    runtime_profile: str = "standard"
    runtime_type: str = "hermes"
    permission_policy_id: str | None = None
    system_prompt: str | None = None
    enabled_toolsets: list[str] | None = None
    integration_configs: dict[str, dict] | None = None


class AgentBuilderTurn(BaseModel):
    assistant_text: str
    draft: AgentDraft
    required_connectors: list[RequiredConnector] = []
    ready_to_create: bool = False


class AgentBuilderSessionCreated(BaseModel):
    session_id: str
    tool_mode: str = "native"


class AgentBuilderFinalizeResult(BaseModel):
    agent_id: str
    agent_name: str
    required_connectors: list[RequiredConnector] = []
