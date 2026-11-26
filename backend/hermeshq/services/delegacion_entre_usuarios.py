from __future__ import annotations

import re
import time
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.task import Task
from hermeshq.models.user import User

PREFIJO_CUENTA_DE_SERVICIO = "bridge-"
MODO_BLOQUEAR = "bloquear"
MODO_PERMITIR_CON_APROBACION = "permitir_con_aprobacion_solicitante"
MENSAJE_DELEGACION_BLOQUEADA = (
    "Esa tarea pertenece al agente de otro usuario; no puedo delegársela. "
    "Pídele al usuario que se lo solicite directamente a su agente."
)
MENSAJE_CONTACTO_BLOQUEADO = (
    "Ese agente pertenece a otro usuario; no puedo contactarlo. "
    "Pídele al usuario que se lo solicite directamente a su agente."
)
_PENDIENTE_TTL = 3600
_pendientes: dict[tuple[str, str], dict] = {}
_autorizados: dict[tuple[str, str], str] = {}

_APROBACION = {
    "si",
    "sip",
    "ok",
    "okay",
    "oka",
    "dale",
    "listo",
    "confirmo",
    "confirmado",
    "adelante",
    "perfecto",
    "correcto",
    "autorizo",
    "autorizado",
    "autorizada",
    "hazlo",
    "yes",
    "procede",
}
_RELLENO = {"de", "acuerdo", "asi", "ya", "por", "favor", "porfa", "nomas", "lo", "la", "le", "ahora", "te"}


def _mensaje_del_usuario(prompt: str) -> str:
    texto = re.sub(r"(?s)^\[Respuesta corta a tu último mensaje:.*?no a temas anteriores\.\]\s*", "", prompt or "")
    texto = re.sub(r"(?s)\[(Hora actual|Archivos adjuntos|Archivos de turnos)[^\n]*?\]", " ", texto)
    return texto.strip()


def es_autorizacion_pura(prompt: str) -> bool:
    texto = _mensaje_del_usuario(prompt)
    if not texto or len(texto) > 60 or "?" in texto:
        return False
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    palabras = re.findall(r"[a-z]+", sin_tildes)
    if not palabras or not any(p in _APROBACION for p in palabras):
        return False
    return all(p in _APROBACION or p in _RELLENO for p in palabras)


async def duenos_por_agente(db: AsyncSession) -> dict[str, set[str]]:
    filas = await db.execute(
        select(AgentAssignment.agent_id, User.id, User.username, User.role).join(
            User, User.id == AgentAssignment.user_id
        )
    )
    duenos: dict[str, set[str]] = {}
    for agent_id, user_id, username, role in filas.all():
        if role == "admin" or es_cuenta_de_servicio(username):
            continue
        duenos.setdefault(agent_id, set()).add(user_id)
    return duenos


def es_cuenta_de_servicio(username: str | None) -> bool:
    return str(username or "").startswith(PREFIJO_CUENTA_DE_SERVICIO)


def modo_delegacion_entre_usuarios() -> str:
    modo = str(getattr(get_settings(), "cross_user_delegation_mode", MODO_BLOQUEAR) or "").strip().lower()
    return MODO_PERMITIR_CON_APROBACION if modo == MODO_PERMITIR_CON_APROBACION else MODO_BLOQUEAR


async def duenos_humanos_por_agente(db: AsyncSession) -> dict[str, set[str]]:
    filas = await db.execute(
        select(AgentAssignment.agent_id, User.id, User.username).join(User, User.id == AgentAssignment.user_id)
    )
    duenos: dict[str, set[str]] = {}
    for agent_id, user_id, username in filas.all():
        if es_cuenta_de_servicio(username):
            continue
        duenos.setdefault(agent_id, set()).add(user_id)
    return duenos


def duenos_distintos(duenos: dict[str, set[str]], origen: Agent, destino: Agent) -> bool:
    propios = duenos.get(destino.id) or set()
    if not propios:
        return False
    return propios != (duenos.get(origen.id) or set())


def es_de_otro_usuario(duenos: dict[str, set[str]], origen: Agent, destino: Agent) -> bool:
    propios = duenos.get(destino.id) or set()
    if destino.is_system_agent or not propios:
        return False
    return not (propios & (duenos.get(origen.id) or set()))


def _mensaje_pedir_autorizacion(destino_nombre: str) -> str:
    return (
        f"Requiere autorización de tu usuario: {destino_nombre} es el agente de OTRA persona y le llegaría un aviso. "
        "No lo contactes por tu cuenta. Pregúntale a TU usuario, con esta forma: «¿Autorizas que le pida a "
        f"{destino_nombre} (agente de otra persona) que <qué le pedirías>?» y termina tu respuesta ahí. Solo si responde que sí, vuelve a hacer la misma llamada. Si no puedes resolver "
        "algo, díselo a tu usuario en vez de pedírselo a otros agentes."
    )


async def autorizacion_pendiente(
    db: AsyncSession, origen: Agent, destino: Agent, destino_nombre: str, tarea_id: str | None
) -> str | None:
    from hermeshq.services.agent_supervisor import conversation_key

    clave = (origen.id, destino.id)
    ahora = time.time()
    tarea = await db.get(Task, tarea_id) if tarea_id else None
    if tarea is not None and tarea.agent_id != origen.id:
        tarea = None
    metadata = (tarea.metadata_json or {}) if tarea is not None else {}
    chat_con_usuario = (
        tarea is not None
        and metadata.get("source") == "public_chat"
        and not metadata.get("scheduled")
        and not metadata.get("background_job")
    )
    if not chat_con_usuario:
        return (
            f"No puedes contactar a {destino_nombre}: es el agente de otra persona y esta tarea no tiene un usuario "
            "que lo autorice. Informa el problema a tu usuario."
        )
    if _autorizados.get(clave) == tarea.id:
        return None
    usuario = str(metadata.get("thread_user_id") or "").strip()
    conversacion = conversation_key(metadata)
    pendiente = _pendientes.get(clave)
    misma_conversacion = (
        pendiente is not None
        and bool(usuario)
        and bool(conversacion)
        and pendiente.get("usuario") == usuario
        and pendiente.get("conversacion") == conversacion
    )
    if misma_conversacion and ahora - pendiente["ts"] <= _PENDIENTE_TTL and pendiente["task"] != tarea.id:
        anterior = await db.execute(
            select(Task.id)
            .where(Task.agent_id == origen.id, Task.queued_at < tarea.queued_at, Task.id != tarea.id)
            .order_by(Task.queued_at.desc())
            .limit(1)
        )
        if anterior.scalar_one_or_none() == pendiente["task"] and es_autorizacion_pura(tarea.prompt or ""):
            _pendientes.pop(clave, None)
            _autorizados[clave] = tarea.id
            return None
    _pendientes[clave] = {"task": tarea.id, "ts": ahora, "usuario": usuario, "conversacion": conversacion}
    return _mensaje_pedir_autorizacion(destino_nombre)
