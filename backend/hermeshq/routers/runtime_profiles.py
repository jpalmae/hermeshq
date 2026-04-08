from fastapi import APIRouter, Depends

from hermeshq.core.security import get_current_user
from hermeshq.models.user import User
from hermeshq.schemas.runtime_profile import RuntimeCapabilityOverviewRead, RuntimeProfileRead
from hermeshq.services.runtime_capabilities import build_runtime_capability_overview
from hermeshq.services.runtime_profiles import list_runtime_profiles

router = APIRouter(prefix="/runtime-profiles", tags=["runtime-profiles"])


@router.get("", response_model=list[RuntimeProfileRead])
async def get_runtime_profiles(
    _: User = Depends(get_current_user),
) -> list[RuntimeProfileRead]:
    return [RuntimeProfileRead.model_validate(item) for item in list_runtime_profiles()]


@router.get("/overview", response_model=RuntimeCapabilityOverviewRead)
async def get_runtime_capability_overview(
    _: User = Depends(get_current_user),
) -> RuntimeCapabilityOverviewRead:
    return RuntimeCapabilityOverviewRead.model_validate(build_runtime_capability_overview())
