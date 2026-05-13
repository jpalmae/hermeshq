from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.mcp_access import McpAccessToken
from hermeshq.models.user import User
from hermeshq.schemas.mcp_access import (
    McpAccessTokenCreate,
    McpAccessTokenCreateResult,
    McpAccessTokenRead,
    McpAccessTokenUpdate,
)
from hermeshq.services.mcp_access import (
    generate_mcp_token,
    hash_mcp_token,
    normalize_mcp_scopes,
    token_display_prefix,
    validate_mcp_agent_ids,
)

router = APIRouter(prefix="/mcp-access", tags=["mcp-access"])


async def _log_mcp_admin_event(
    db: AsyncSession,
    event_type: str,
    *,
    current_user: User,
    access: McpAccessToken,
    message: str,
    details: dict | None = None,
) -> None:
    db.add(
        ActivityLog(
            event_type=event_type,
            severity="info",
            message=message,
            details={
                "mcp_access_token_id": access.id,
                "mcp_access_token_name": access.name,
                "actor_user_id": current_user.id,
                **(details or {}),
            },
        )
    )


@router.get("/access-tokens", response_model=list[McpAccessTokenRead])
async def list_mcp_access_tokens(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[McpAccessTokenRead]:
    result = await db.execute(select(McpAccessToken).order_by(desc(McpAccessToken.created_at)))
    return [McpAccessTokenRead.model_validate(access) for access in result.scalars().all()]


@router.post("/access-tokens", response_model=McpAccessTokenCreateResult, status_code=status.HTTP_201_CREATED)
async def create_mcp_access_token(
    payload: McpAccessTokenCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> McpAccessTokenCreateResult:
    agent_ids = await validate_mcp_agent_ids(db, payload.allowed_agent_ids)
    token = generate_mcp_token()
    access = McpAccessToken(
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        client_name=(payload.client_name or "").strip() or None,
        token_prefix=token_display_prefix(token),
        token_hash=hash_mcp_token(token),
        created_by_user_id=current_user.id,
        allowed_agent_ids=agent_ids,
        scopes=normalize_mcp_scopes(payload.scopes),
        expires_at=payload.expires_at,
        is_active=True,
    )
    db.add(access)
    await db.flush()
    await _log_mcp_admin_event(
        db,
        "mcp.access_token.created",
        current_user=current_user,
        access=access,
        message=f"MCP access token created: {access.name}",
        details={"allowed_agent_ids": agent_ids, "scopes": access.scopes},
    )
    await db.commit()
    await db.refresh(access)
    return McpAccessTokenCreateResult(
        token=token,
        access=McpAccessTokenRead.model_validate(access),
    )


@router.patch("/access-tokens/{access_token_id}", response_model=McpAccessTokenRead)
async def update_mcp_access_token(
    access_token_id: str,
    payload: McpAccessTokenUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> McpAccessTokenRead:
    access = await db.get(McpAccessToken, access_token_id)
    if not access:
        raise HTTPException(status_code=404, detail="MCP access token not found")
    if payload.name is not None:
        access.name = payload.name.strip()
    if payload.description is not None:
        access.description = payload.description.strip() or None
    if payload.client_name is not None:
        access.client_name = payload.client_name.strip() or None
    if payload.allowed_agent_ids is not None:
        access.allowed_agent_ids = await validate_mcp_agent_ids(db, payload.allowed_agent_ids)
    if payload.scopes is not None:
        access.scopes = normalize_mcp_scopes(payload.scopes)
    if payload.is_active is not None:
        access.is_active = payload.is_active
    if "expires_at" in payload.model_fields_set:
        access.expires_at = payload.expires_at
    await _log_mcp_admin_event(
        db,
        "mcp.access_token.updated",
        current_user=current_user,
        access=access,
        message=f"MCP access token updated: {access.name}",
        details={"is_active": access.is_active, "allowed_agent_ids": access.allowed_agent_ids, "scopes": access.scopes},
    )
    await db.commit()
    await db.refresh(access)
    return McpAccessTokenRead.model_validate(access)


@router.delete("/access-tokens/{access_token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_access_token(
    access_token_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    access = await db.get(McpAccessToken, access_token_id)
    if not access:
        raise HTTPException(status_code=404, detail="MCP access token not found")
    access.is_active = False
    access.expires_at = datetime.now(timezone.utc)
    await _log_mcp_admin_event(
        db,
        "mcp.access_token.revoked",
        current_user=current_user,
        access=access,
        message=f"MCP access token revoked: {access.name}",
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/access-tokens/{access_token_id}/rotate", response_model=McpAccessTokenCreateResult)
async def rotate_mcp_access_token(
    access_token_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> McpAccessTokenCreateResult:
    """Rotate an MCP access token — generates a new secret while keeping the same config.

    The old token is immediately invalidated. The new token inherits all
    scopes, agent permissions, and settings. The expiration is extended by
    the same duration if the token hasn't expired yet.
    """
    access = await db.get(McpAccessToken, access_token_id)
    if not access:
        raise HTTPException(status_code=404, detail="MCP access token not found")

    # Generate new token
    new_raw_token = generate_mcp_token()
    access.token_prefix = token_display_prefix(new_raw_token)
    access.token_hash = hash_mcp_token(new_raw_token)

    # Extend expiration if applicable
    if access.expires_at:
        from datetime import timedelta

        remaining = access.expires_at - datetime.now(timezone.utc)
        if remaining.total_seconds() > 0:
            access.expires_at = datetime.now(timezone.utc) + remaining
        else:
            # Already expired — give 30 days
            access.expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    await _log_mcp_admin_event(
        db,
        "mcp.access_token.rotated",
        current_user=current_user,
        access=access,
        message=f"MCP access token rotated: {access.name}",
    )
    await db.commit()
    await db.refresh(access)
    return McpAccessTokenCreateResult(
        token=new_raw_token,
        access=McpAccessTokenRead.model_validate(access),
    )
