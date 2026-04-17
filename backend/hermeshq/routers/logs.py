from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, and_, cast, desc, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.user import User
from hermeshq.schemas.activity import ActivityPageRead, ActivityRead

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_model=ActivityPageRead)
async def list_logs(
    agent_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    query: str | None = Query(default=None),
    before_created_at: datetime | None = Query(default=None),
    before_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ActivityPageRead:
    statement = select(ActivityLog)
    if agent_id:
        await ensure_agent_access(db, current_user, agent_id)
        statement = statement.where(ActivityLog.agent_id == agent_id)
    elif not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(ActivityLog.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    if task_id:
        statement = statement.where(ActivityLog.task_id == task_id)
    normalized_query = (query or "").strip()
    if normalized_query:
        pattern = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                ActivityLog.event_type.ilike(pattern),
                ActivityLog.message.ilike(pattern),
                cast(ActivityLog.details, String).ilike(pattern),
                cast(ActivityLog.created_at, String).ilike(pattern),
            )
        )
    if before_created_at:
        cursor_id = before_id or ""
        statement = statement.where(
            or_(
                ActivityLog.created_at < before_created_at,
                and_(ActivityLog.created_at == before_created_at, ActivityLog.id < cursor_id),
            )
        )
    rows_to_fetch = limit + 1
    result = await db.execute(
        statement.order_by(desc(ActivityLog.created_at), desc(ActivityLog.id)).limit(rows_to_fetch)
    )
    items = result.scalars().all()
    has_more = len(items) > limit
    page_items = items[:limit]
    next_before_created_at = page_items[-1].created_at if has_more and page_items else None
    next_before_id = page_items[-1].id if has_more and page_items else None
    return ActivityPageRead(
        items=[ActivityRead.model_validate(item) for item in page_items],
        has_more=has_more,
        next_before_created_at=next_before_created_at,
        next_before_id=next_before_id,
    )
