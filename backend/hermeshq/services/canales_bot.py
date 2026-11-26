from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity

logger = logging.getLogger(__name__)


def _filas(resultado, modelo) -> list:
    return [fila for fila in resultado.scalars().all() if isinstance(fila, modelo)]


async def canal_por_defecto(db: AsyncSession) -> BotChannel | None:
    resultado = await db.execute(select(BotChannel).where(BotChannel.is_default.is_(True)).limit(1))
    filas = _filas(resultado, BotChannel)
    return filas[0] if filas else None


async def canal_de_clave(db: AsyncSession, api_key) -> BotChannel | None:
    canal_id = getattr(api_key, "bot_channel_id", None)
    if canal_id:
        canal = await db.get(BotChannel, canal_id)
        return canal if isinstance(canal, BotChannel) else None
    return await canal_por_defecto(db)


async def id_canal_efectivo(db: AsyncSession, api_key) -> tuple[str | None, bool]:
    canal_id = getattr(api_key, "bot_channel_id", None)
    defecto = await canal_por_defecto(db)
    if not canal_id:
        return (defecto.id if defecto else None), True
    return str(canal_id), bool(defecto and str(defecto.id) == str(canal_id))


async def identidades_de_usuario(db: AsyncSession, user_id: str) -> list[UserChannelIdentity]:
    resultado = await db.execute(
        select(UserChannelIdentity).where(
            UserChannelIdentity.user_id == user_id, UserChannelIdentity.is_active.is_(True)
        )
    )
    return _filas(resultado, UserChannelIdentity)


def identidad_en_canal(identidades: list, canal_id: str | None) -> UserChannelIdentity | None:
    if not canal_id:
        return None
    return next((i for i in identidades if str(i.bot_channel_id) == str(canal_id)), None)


async def usuarios_por_identidad(
    db: AsyncSession, canal_id: str, external_user_id: str | None, aad_object_id: str | None
) -> tuple[list[str], bool]:
    for campo, valor in (
        (UserChannelIdentity.external_user_id, (external_user_id or "").strip()),
        (UserChannelIdentity.aad_object_id, (aad_object_id or "").strip()),
    ):
        if not valor:
            continue
        resultado = await db.execute(
            select(UserChannelIdentity).where(
                UserChannelIdentity.bot_channel_id == canal_id,
                campo == valor,
                UserChannelIdentity.is_active.is_(True),
            )
        )
        usuarios = sorted({str(i.user_id) for i in _filas(resultado, UserChannelIdentity)})
        if usuarios:
            return usuarios, True
    return [], False
