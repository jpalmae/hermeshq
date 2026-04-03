from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.user import User
from hermeshq.schemas.activity import ActivityRead

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_model=list[ActivityRead])
async def list_logs(
    agent_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ActivityRead]:
    statement = select(ActivityLog)
    if agent_id:
        await ensure_agent_access(db, current_user, agent_id)
        statement = statement.where(ActivityLog.agent_id == agent_id)
    elif not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(ActivityLog.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    if task_id:
        statement = statement.where(ActivityLog.task_id == task_id)
    result = await db.execute(statement.order_by(desc(ActivityLog.created_at)).limit(limit))
    return [ActivityRead.model_validate(item) for item in result.scalars().all()]
