from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.base import utcnow
from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity
from hermeshq.models.user import User
from hermeshq.schemas.common import ORMModel

SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

canales_router = APIRouter(prefix="/admin/bot-channels", tags=["bot-channels"])
identidades_router = APIRouter(prefix="/users", tags=["users"])


class BotChannelRead(ORMModel):
    id: str
    slug: str
    platform: str
    display_name: str
    azure_app_id: str | None = None
    tenant_id: str | None = None
    service_user_id: str | None = None
    is_default: bool
    is_active: bool
    require_channel_header: bool
    created_at: datetime
    updated_at: datetime


class BotChannelCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="microsoft_teams", pattern="^microsoft_teams$")
    azure_app_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = Field(default=None, max_length=64)
    service_user_id: str | None = None
    is_default: bool = False
    is_active: bool = True
    require_channel_header: bool = False

    @field_validator("slug")
    @classmethod
    def _slug(cls, valor: str) -> str:
        valor = valor.strip().lower()
        if not SLUG_VALIDO.match(valor):
            raise ValueError("El slug solo admite minúsculas, números y guiones")
        return valor


class BotChannelUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    azure_app_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = Field(default=None, max_length=64)
    service_user_id: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    require_channel_header: bool | None = None


class IdentidadRead(ORMModel):
    id: str
    user_id: str
    bot_channel_id: str
    external_user_id: str | None = None
    aad_object_id: str | None = None
    tenant_id: str | None = None
    is_active: bool
    verified_at: datetime | None = None
    created_by_user_id: str | None = None
    created_at: datetime


class IdentidadCreate(BaseModel):
    bot_channel_id: str
    external_user_id: str | None = Field(default=None, max_length=255)
    aad_object_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = Field(default=None, max_length=64)
    verified: bool = False


async def _usuario_de_servicio(db: AsyncSession, user_id: str | None) -> None:
    if user_id and await db.get(User, user_id) is None:
        raise HTTPException(status_code=422, detail="service_user_id no existe")


async def _quitar_otro_por_defecto(db: AsyncSession, canal_id: str | None) -> None:
    consulta = update(BotChannel).where(BotChannel.is_default.is_(True))
    if canal_id:
        consulta = consulta.where(BotChannel.id != canal_id)
    await db.execute(consulta.values(is_default=False))


@canales_router.get("", response_model=list[BotChannelRead])
async def listar_canales(_admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    resultado = await db.execute(select(BotChannel).order_by(BotChannel.created_at.asc()))
    return [BotChannelRead.model_validate(c) for c in resultado.scalars().all()]


@canales_router.post("", response_model=BotChannelRead, status_code=status.HTTP_201_CREATED)
async def crear_canal(
    payload: BotChannelCreate, _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    await _usuario_de_servicio(db, payload.service_user_id)
    if payload.is_default:
        await _quitar_otro_por_defecto(db, None)
    canal = BotChannel(**payload.model_dump())
    db.add(canal)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Ya existe un canal con ese slug")
    await db.refresh(canal)
    return BotChannelRead.model_validate(canal)


@canales_router.patch("/{canal_id}", response_model=BotChannelRead)
async def editar_canal(
    canal_id: str,
    payload: BotChannelUpdate,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    canal = await db.get(BotChannel, canal_id)
    if canal is None:
        raise HTTPException(status_code=404, detail="Canal no encontrado")
    cambios = payload.model_dump(exclude_unset=True)
    if "service_user_id" in cambios:
        await _usuario_de_servicio(db, cambios["service_user_id"])
    if cambios.get("is_default") is False and canal.is_default:
        raise HTTPException(status_code=409, detail="Marca otro canal como predeterminado en lugar de quitarlo")
    if cambios.get("is_default"):
        await _quitar_otro_por_defecto(db, canal.id)
    for campo, valor in cambios.items():
        setattr(canal, campo, valor)
    await db.commit()
    await db.refresh(canal)
    return BotChannelRead.model_validate(canal)


async def _usuario(db: AsyncSession, user_id: str) -> User:
    usuario = await db.get(User, user_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="User not found")
    return usuario


@identidades_router.get("/{user_id}/channel-identities", response_model=list[IdentidadRead])
async def listar_identidades(
    user_id: str, _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    await _usuario(db, user_id)
    resultado = await db.execute(
        select(UserChannelIdentity)
        .where(UserChannelIdentity.user_id == user_id)
        .order_by(UserChannelIdentity.created_at.asc())
    )
    return [IdentidadRead.model_validate(i) for i in resultado.scalars().all()]


@identidades_router.post(
    "/{user_id}/channel-identities", response_model=IdentidadRead, status_code=status.HTTP_201_CREATED
)
async def crear_identidad(
    user_id: str,
    payload: IdentidadCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    await _usuario(db, user_id)
    if await db.get(BotChannel, payload.bot_channel_id) is None:
        raise HTTPException(status_code=422, detail="El canal de bot no existe")
    externo = (payload.external_user_id or "").strip() or None
    aad = (payload.aad_object_id or "").strip() or None
    tenant = (payload.tenant_id or "").strip() or None
    if not externo and not aad:
        raise HTTPException(status_code=422, detail="Indica external_user_id (29:...) o aad_object_id")
    choques = []
    if externo:
        choques.append(UserChannelIdentity.external_user_id == externo)
    if aad:
        choques.append(UserChannelIdentity.aad_object_id == aad)
    for choque in choques:
        existente = await db.execute(
            select(UserChannelIdentity.id).where(UserChannelIdentity.bot_channel_id == payload.bot_channel_id, choque)
        )
        if existente.first() is not None:
            raise HTTPException(status_code=409, detail="Esa identidad ya está vinculada en este canal")
    identidad = UserChannelIdentity(
        user_id=user_id,
        bot_channel_id=payload.bot_channel_id,
        external_user_id=externo,
        aad_object_id=aad,
        tenant_id=tenant,
        verified_at=utcnow() if payload.verified else None,
        created_by_user_id=admin.id,
    )
    db.add(identidad)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Esa identidad ya está vinculada en este canal")
    await db.refresh(identidad)
    return IdentidadRead.model_validate(identidad)


@identidades_router.delete("/{user_id}/channel-identities/{identidad_id}", status_code=status.HTTP_204_NO_CONTENT)
async def borrar_identidad(
    user_id: str,
    identidad_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    identidad = await db.get(UserChannelIdentity, identidad_id)
    if identidad is None or identidad.user_id != user_id:
        raise HTTPException(status_code=404, detail="Identidad no encontrada")
    await db.delete(identidad)
    await db.commit()
