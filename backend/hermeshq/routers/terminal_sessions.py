from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.terminal_session import TerminalSession
from hermeshq.models.user import User
from hermeshq.schemas.terminal_session import TerminalSessionRead

router = APIRouter(prefix="/terminal-sessions", tags=["terminal-sessions"])


@router.get("", response_model=list[TerminalSessionRead])
async def list_terminal_sessions(
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TerminalSessionRead]:
    statement = select(TerminalSession)
    if agent_id:
        await ensure_agent_access(db, current_user, agent_id)
        statement = statement.where(TerminalSession.agent_id == agent_id)
    elif not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(TerminalSession.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement.order_by(desc(TerminalSession.started_at)).limit(limit))
    return [TerminalSessionRead.model_validate(item) for item in result.scalars().all()]
