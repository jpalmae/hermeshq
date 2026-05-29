"""Diagnostic endpoints for monitoring system health and database connections."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session, engine
from hermeshq.models.user import User

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class DatabasePoolStatus(BaseModel):
    pool_size: int
    max_overflow: int
    checked_in: int
    checked_out: int
    overflow: int
    total_connections: int
    total_available: int


class DatabaseConnectionStats(BaseModel):
    total_connections: int
    idle_connections: int
    active_connections: int
    max_idle_duration_seconds: int | None
    connection_details: list[dict]


@router.get("/db-pool", response_model=DatabasePoolStatus)
async def get_db_pool_status(
    _: User = Depends(require_admin),
) -> DatabasePoolStatus:
    """Get SQLAlchemy connection pool status."""
    pool = engine.pool
    checked_in = pool.checkedin()
    checked_out = pool.checkedout()
    overflow = pool.overflow()
    size = pool.size()

    return DatabasePoolStatus(
        pool_size=size,
        max_overflow=pool.max_overflow,
        checked_in=checked_in,
        checked_out=checked_out,
        overflow=overflow,
        total_connections=checked_in + checked_out,
        total_available=size - checked_out,
    )


@router.get("/db-connections", response_model=DatabaseConnectionStats)
async def get_db_connections(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> DatabaseConnectionStats:
    """Get detailed PostgreSQL connection statistics."""
    query = text("""
        SELECT
            state,
            count(*) as count,
            max(extract(epoch from (now() - state_change)))::int as max_idle_seconds,
            string_agg(application_name, ', ') as app_names
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state
    """)
    result = await db.execute(query)
    stats = result.mappings().all()

    total = sum(s["count"] for s in stats)
    idle = next((s["count"] for s in stats if s["state"] == "idle"), 0)
    active = next((s["count"] for s in stats if s["state"] != "idle"), 0)
    max_idle = max(
        (s["max_idle_seconds"] for s in stats if s["state"] == "idle"),
        default=None,
    )

    return DatabaseConnectionStats(
        total_connections=total,
        idle_connections=idle,
        active_connections=active,
        max_idle_duration_seconds=max_idle,
        connection_details=[
            {
                "state": s["state"],
                "count": s["count"],
                "max_idle_seconds": s["max_idle_seconds"],
                "applications": s["app_names"],
            }
            for s in stats
        ],
    )


@router.post("/db-recycle-stale")
async def recycle_stale_connections(
    _: User = Depends(require_admin),
) -> dict:
    """Force recycling of idle connections in the pool (development only)."""
    pool = engine.pool
    # This will close all connections
    await pool.dispose()
    return {"message": "Connection pool recycled", "pool_size": pool.size()}
