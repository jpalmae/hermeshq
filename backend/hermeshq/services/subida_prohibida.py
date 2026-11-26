from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.task import Task

NO_SUBIR = re.compile(
    r"\bno\s+(?:me\s+)?(?:lo|la|los|las)?\s*(?:subas|suban|subir|guardes|guarden|cargues|carguen)\b"
    r"(?:\s+(?:todavia|aun|por\s+ahora))?\s*"
    r"(?:(?:a|en|al)\s+(?:mi\s+|tu\s+|su\s+|el\s+|la\s+)?(?:onedrive|sharepoint|nube)\b|[.,;:!)\n]|$)"
)
SOLO_EN_EL_CHAT = re.compile(r"\bsolo\s+(?:adjuntal[oa]s?|adjunta(?:lo|la)?|en\s+el\s+chat|por\s+(?:aqui|el\s+chat))\b")
PIDE_SUBIR_AHORA = re.compile(
    r"\b(?:sub(?:e|elo|ela|elos|elas|ir|irlo|irla)|guard(?:a|alo|ala|alos|alas)\s+en\s+(?:mi\s+|el\s+|la\s+)?"
    r"(?:onedrive|sharepoint|carpeta))\b"
)
VIGENCIA = timedelta(hours=8)


def _plano(texto) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii").lower()


def frase_de_prohibicion(texto) -> str | None:
    plano = _plano(texto)
    coincidencia = NO_SUBIR.search(plano) or SOLO_EN_EL_CHAT.search(plano)
    return coincidencia.group(0).strip(" .,;:!)\n") if coincidencia else None


def _escrito_por_el_usuario(metadata: dict) -> str:
    return str(metadata.get("mensaje_usuario") or metadata.get("pedido_del_usuario") or "")


async def prohibicion_vigente(db: AsyncSession, tarea: Task) -> str | None:
    metadata = tarea.metadata_json or {}
    for texto in (metadata.get("mensaje_usuario"), metadata.get("pedido_del_usuario")):
        frase = frase_de_prohibicion(texto)
        if frase:
            return frase
    if PIDE_SUBIR_AHORA.search(_plano(_escrito_por_el_usuario(metadata))):
        return None
    referencia = tarea
    if metadata.get("origin_task_id"):
        try:
            origen = await db.get(Task, str(uuid.UUID(str(metadata["origin_task_id"]))))
        except ValueError:
            origen = None
        if origen is not None:
            datos_origen = origen.metadata_json or {}
            frase = frase_de_prohibicion(datos_origen.get("mensaje_usuario"))
            if frase:
                return frase
            if PIDE_SUBIR_AHORA.search(_plano(datos_origen.get("mensaje_usuario"))):
                return None
            referencia = origen
            metadata = {**datos_origen, **metadata}
    sesion = str(metadata.get("public_session_id") or metadata.get("origen_public_session_id") or "")
    if not sesion or referencia.queued_at is None:
        return None
    epoca = metadata.get("conversation_epoch")
    anteriores = (
        (
            await db.execute(
                select(Task.metadata_json)
                .where(
                    Task.agent_id == referencia.agent_id,
                    Task.id.notin_([tarea.id, referencia.id]),
                    Task.queued_at < referencia.queued_at,
                    Task.queued_at > referencia.queued_at - VIGENCIA,
                    Task.metadata_json["public_session_id"].as_string() == sesion,
                )
                .order_by(Task.queued_at.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    for previa in anteriores:
        previa = previa or {}
        if epoca is not None and previa.get("conversation_epoch") not in (None, epoca):
            return None
        mensaje = previa.get("mensaje_usuario")
        frase = frase_de_prohibicion(mensaje)
        if frase:
            return frase
        if PIDE_SUBIR_AHORA.search(_plano(mensaje)):
            return None
    return None
