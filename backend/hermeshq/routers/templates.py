from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.template import AgentTemplate
from hermeshq.models.user import User
from hermeshq.schemas.template import TemplateCreate, TemplateRead, TemplateUpdate

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=list[TemplateRead])
async def list_templates(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TemplateRead]:
    result = await db.execute(select(AgentTemplate).order_by(AgentTemplate.created_at.asc()))
    return [TemplateRead.model_validate(item) for item in result.scalars().all()]


@router.post("", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateCreate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TemplateRead:
    template = AgentTemplate(**payload.model_dump())
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return TemplateRead.model_validate(template)


@router.get("/{template_id}", response_model=TemplateRead)
async def get_template(
    template_id: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TemplateRead:
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateRead.model_validate(template)


@router.put("/{template_id}", response_model=TemplateRead)
async def update_template(
    template_id: str,
    payload: TemplateUpdate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TemplateRead:
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    await db.commit()
    await db.refresh(template)
    return TemplateRead.model_validate(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)
    await db.commit()

