from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.user import User

logger = logging.getLogger(__name__)

_PLATFORM_COLUMN: dict[str, str] = {
    "telegram": "telegram_id",
    "whatsapp": "whatsapp_user",
    "microsoft_teams": "teams_id",
    "google_chat": "google_chat_email",
    # kapso_whatsapp: match the user's WhatsApp phone number stored in kapso_number.
    # The sender_id from Kapso webhooks may or may not include the '+' prefix, so we
    # try both forms.
    "kapso_whatsapp": "kapso_number",
}


async def _por_identidad_de_canal(
    db: AsyncSession, bot_channel_id: str | None, sender_id: str, aad_object_id: str | None
) -> tuple[User | None, bool, bool]:
    from hermeshq.services.canales_bot import canal_por_defecto, usuarios_por_identidad

    defecto = await canal_por_defecto(db)
    canal_id = bot_channel_id or (defecto.id if defecto else None)
    es_defecto = not bot_channel_id or bool(defecto and str(defecto.id) == str(bot_channel_id))
    if not canal_id:
        return None, False, es_defecto
    candidatos, hubo = await usuarios_por_identidad(db, canal_id, sender_id, aad_object_id)
    if not hubo:
        return None, False, es_defecto
    activos = []
    for user_id in candidatos:
        user = await db.get(User, user_id)
        if isinstance(user, User) and user.is_active:
            activos.append(user)
    if len(activos) != 1:
        logger.warning("Identidad de Teams ambigua o inactiva en el canal %s; no se resuelve", canal_id)
        return None, True, es_defecto
    return activos[0], True, es_defecto


async def resolve_channel_user(
    db: AsyncSession,
    platform: str,
    sender_id: str,
    *,
    bot_channel_id: str | None = None,
    aad_object_id: str | None = None,
) -> User | None:
    column_name = _PLATFORM_COLUMN.get(platform)
    sender_id = (sender_id or "").strip()
    if platform == "microsoft_teams" and (sender_id or (aad_object_id or "").strip()):
        user, hubo, es_defecto = await _por_identidad_de_canal(db, bot_channel_id, sender_id, aad_object_id)
        if hubo:
            return user
        if not es_defecto:
            return None
    if not column_name or not sender_id:
        return None
    candidates = [sender_id]
    if platform == "kapso_whatsapp":
        alt_id = sender_id[1:] if sender_id.startswith("+") else "+" + sender_id
        if alt_id.strip("+"):
            candidates.append(alt_id)
    column = getattr(User, column_name)
    result = await db.execute(select(User).where(column.in_(candidates), User.is_active.is_(True)).limit(2))
    users = list(result.scalars().all())
    if len(users) > 1:
        logger.warning("Identificador de %s asignado a más de un usuario activo; no se resuelve", platform)
        return None
    return users[0] if users else None
