from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.user import User
from hermeshq.schemas.managed_integration import ManagedIntegrationRead
from hermeshq.services.managed_capabilities import list_managed_integrations

router = APIRouter(prefix="/managed-integrations", tags=["managed-integrations"])


@router.get("", response_model=list[ManagedIntegrationRead])
async def get_managed_integrations(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ManagedIntegrationRead]:
    settings = await db.get(AppSettings, "default")
    enabled = getattr(settings, "enabled_integration_packages", []) if settings else []
    return [ManagedIntegrationRead.model_validate(item) for item in list_managed_integrations(enabled)]
