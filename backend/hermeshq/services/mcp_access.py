import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.models.mcp_access import McpAccessToken

TOKEN_PREFIX = "hq_mcp_"
DEFAULT_MCP_SCOPES = {"agents:list", "agents:invoke", "tasks:read"}


def generate_mcp_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def hash_mcp_token(token: str) -> str:
    digest = hmac.new(
        get_settings().jwt_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def token_display_prefix(token: str) -> str:
    return token[:18]


def normalize_mcp_scopes(scopes: list[str] | None) -> list[str]:
    values = list(dict.fromkeys(str(scope).strip() for scope in (scopes or []) if str(scope).strip()))
    invalid = [scope for scope in values if scope not in DEFAULT_MCP_SCOPES]
    if invalid:
        allowed = ", ".join(sorted(DEFAULT_MCP_SCOPES))
        raise HTTPException(status_code=400, detail=f"Invalid MCP scopes: {', '.join(invalid)}. Allowed: {allowed}")
    return values or sorted(DEFAULT_MCP_SCOPES)


async def validate_mcp_agent_ids(db: AsyncSession, agent_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()))
    if not normalized:
        return []
    result = await db.execute(select(Agent.id).where(Agent.id.in_(normalized), Agent.is_archived.is_(False)))
    existing = set(result.scalars().all())
    missing = [agent_id for agent_id in normalized if agent_id not in existing]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown or archived agent ids: {', '.join(missing)}")
    return normalized


async def authenticate_mcp_token(db: AsyncSession, authorization: str | None) -> McpAccessToken:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing MCP bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_hash = hash_mcp_token(token.strip())
    result = await db.execute(select(McpAccessToken).where(McpAccessToken.token_hash == token_hash))
    access = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not access or not access.is_active or (access.expires_at and access.expires_at <= now):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MCP token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access.last_used_at = now
    return access


def ensure_mcp_scope(access: McpAccessToken, scope: str) -> None:
    if scope not in (access.scopes or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"MCP token lacks scope: {scope}")


def ensure_mcp_agent_allowed(access: McpAccessToken, agent_id: str) -> None:
    if agent_id not in (access.allowed_agent_ids or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MCP token cannot access this agent")
