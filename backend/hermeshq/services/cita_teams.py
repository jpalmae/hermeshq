from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from hermeshq.models.public_chat import PublicChatMessage, PublicChatNotification, PublicChatSession
from hermeshq.models.task import Task

MARCA_CITA = re.compile(r"<quoted\s+messageId\s*=\s*\"(\d{10,16})\"\s*/?>(?:\s*</quoted>)?", re.IGNORECASE)
NOTIF_ANTES = timedelta(seconds=15)
NOTIF_DESPUES = timedelta(minutes=5)
RESPUESTA_ANTES = timedelta(minutes=5)
RESPUESTA_DESPUES = timedelta(seconds=15)
RESPUESTA_CERCANA = timedelta(seconds=10)
DESFASE_RELOJ = timedelta(seconds=3)
NOTIF_CERCANA = timedelta(seconds=30)
PROPIO_DESPUES = timedelta(minutes=2)
RESPUESTA_POSTERIOR = timedelta(milliseconds=500)
MAX_TEXTO_CITADO = 3500


@dataclass
class Cita:
    instante: datetime | None
    texto_usuario: str
    tipo: str = ""
    task_id: str = ""
    notification_id: str = ""
    titulo: str = ""
    texto: str = ""

    @property
    def resuelta(self) -> bool:
        return bool(self.texto)

    def prefijo(self) -> str:
        if not self.resuelta:
            return (
                "[El usuario está citando en Teams un mensaje anterior que no pude identificar. Si su pedido no se "
                "entiende sin ese mensaje, pídele que lo pegue o que diga cuál es; no inventes su contenido.]"
            )
        from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, resolve_timezone

        fecha = self.instante.astimezone(resolve_timezone(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")
        if self.tipo == "propio":
            return (
                f"[El usuario está citando en Teams SU PROPIO mensaje anterior, que te envió el {fecha}. "
                f"Texto de su mensaje citado:\n«{self.texto}»\n"
                "Esa es la instrucción a la que se refiere: tómala tal cual, no la busques en el correo ni en "
                "otras herramientas. Si te pidió algo en ese mensaje y no está hecho, hazlo ahora; si ya lo "
                "hiciste, muestra lo que entregaste.]"
            )
        if self.tipo == "programada":
            origen = f"el resultado de la tarea programada «{self.titulo}» (task_id={self.task_id})"
        else:
            origen = f"tu respuesta anterior (task_id={self.task_id})"
        return (
            f"[El usuario está citando en Teams un mensaje que este chat le entregó el {fecha}: {origen}. "
            f"Texto citado:\n«{self.texto}»\n"
            "Su mensaje se refiere a ESE contenido («la 1», «agrega una columna», etc.): trabaja sobre él; "
            "no digas que no tienes el contenido. Si pide cambiarle la forma (agregar una columna o un número, "
            "pasarlo a tabla, ordenarlo, resumirlo), reescríbelo tú mismo con ese texto, sin pedir confirmación. "
            "Si pide el detalle de un elemento, búscalo con tus herramientas y muéstralo, sin preguntar si debes "
            "hacerlo.]"
        )


def extraer_cita(content: str | None) -> tuple[bool, datetime | None, str]:
    texto = content or ""
    marca = MARCA_CITA.search(texto)
    if not marca:
        return False, None, texto
    try:
        instante = datetime.fromtimestamp(int(marca.group(1)) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        instante = None
    return True, instante, MARCA_CITA.sub("", texto).strip()


def _md(item) -> dict:
    return getattr(item, "metadata_json", None) or {}


def _texto(*partes: str | None) -> str:
    texto = "\n\n".join(p.strip() for p in partes if p and p.strip())
    if len(texto) > MAX_TEXTO_CITADO:
        texto = texto[:MAX_TEXTO_CITADO] + " […]"
    return texto


def _notificacion_valida(n, agent_id: str, api_key_id: str, thread_user_id: str) -> bool:
    return (
        getattr(n, "status", None) == "delivered"
        and getattr(n, "delivered_at", None) is not None
        and str(n.agent_id) == str(agent_id)
        and str(n.api_key_id) == str(api_key_id)
        and str(n.recipient_user_id or "") == str(thread_user_id)
    )


def _respuesta_valida(t, agent_id: str, api_key_id: str, thread_user_id: str) -> bool:
    md = _md(t)
    return (
        str(t.agent_id) == str(agent_id)
        and t.status == "completed"
        and t.completed_at is not None
        and bool((t.response or "").strip())
        and md.get("conversation")
        and not md.get("reset")
        and not md.get("auto_reset")
        and md.get("thread_user_id") == thread_user_id
        and str(md.get("public_chat_api_key_id") or "") == str(api_key_id)
    )


def elegir(
    instante: datetime,
    notificaciones: list,
    respuestas: list,
    agent_id: str,
    api_key_id: str,
    thread_user_id: str,
    propios: list | None = None,
) -> tuple | None:
    cercanos, lejanos, del_usuario = [], [], []
    for n in notificaciones:
        if not _notificacion_valida(n, agent_id, api_key_id, thread_user_id):
            continue
        delta = n.delivered_at - instante
        if -NOTIF_ANTES <= delta <= NOTIF_DESPUES:
            destino = cercanos if -DESFASE_RELOJ <= delta <= NOTIF_CERCANA else lejanos
            destino.append((abs(delta), "programada", n))
    for t in respuestas:
        if not _respuesta_valida(t, agent_id, api_key_id, thread_user_id):
            continue
        delta = instante - t.completed_at
        if -RESPUESTA_DESPUES <= delta <= RESPUESTA_ANTES:
            if -DESFASE_RELOJ <= delta <= RESPUESTA_CERCANA:
                cercanos.append((abs(delta), "respuesta", t))
            else:
                lejanos.append((abs(delta), "respuesta", t))
    for m in propios or []:
        if getattr(m, "role", None) != "user" or not (m.content or "").strip():
            continue
        delta = m.created_at - instante
        if -DESFASE_RELOJ <= delta <= PROPIO_DESPUES:
            del_usuario.append((abs(delta), "propio", m))
    if del_usuario:
        llegada = min(c[2].created_at for c in del_usuario)

        def _respondio_a_ese_mensaje(candidato) -> bool:
            return candidato[1] == "respuesta" and candidato[2].completed_at >= llegada - RESPUESTA_POSTERIOR

        cercanos = [c for c in cercanos if not _respondio_a_ese_mensaje(c)]
        lejanos = [c for c in lejanos if not _respondio_a_ese_mensaje(c)]
    for grupo in (cercanos, del_usuario, lejanos):
        if grupo:
            return sorted(grupo, key=lambda c: c[0])[0]
    return None


def _texto_propio(contenido: str) -> str:
    return _texto(MARCA_CITA.sub("", contenido or ""))


async def resolver_cita(db, agent_id: str, api_key_id: str | None, thread_user_id: str, content: str) -> Cita | None:
    hay, instante, resto = extraer_cita(content)
    if not hay:
        return None
    cita = Cita(instante=instante, texto_usuario=resto)
    if instante is None or not api_key_id or not thread_user_id or thread_user_id.startswith("__"):
        return cita
    notificaciones = (
        (
            await db.execute(
                select(PublicChatNotification).where(
                    PublicChatNotification.agent_id == agent_id,
                    PublicChatNotification.api_key_id == api_key_id,
                    PublicChatNotification.recipient_user_id == thread_user_id,
                    PublicChatNotification.status == "delivered",
                    PublicChatNotification.delivered_at >= instante - NOTIF_ANTES,
                    PublicChatNotification.delivered_at <= instante + NOTIF_DESPUES,
                )
            )
        )
        .scalars()
        .all()
    )
    respuestas = (
        (
            await db.execute(
                select(Task).where(
                    Task.agent_id == agent_id,
                    Task.status == "completed",
                    Task.completed_at >= instante - RESPUESTA_ANTES,
                    Task.completed_at <= instante + RESPUESTA_DESPUES,
                )
            )
        )
        .scalars()
        .all()
    )
    marca_actual = f'messageId="{int(instante.timestamp() * 1000)}"'
    propios = [
        m
        for m in (
            await db.execute(
                select(PublicChatMessage)
                .join(PublicChatSession, PublicChatSession.id == PublicChatMessage.session_id)
                .where(
                    PublicChatSession.agent_id == agent_id,
                    PublicChatSession.api_key_id == api_key_id,
                    PublicChatSession.user_id == thread_user_id,
                    PublicChatMessage.role == "user",
                    PublicChatMessage.created_at >= instante - DESFASE_RELOJ,
                    PublicChatMessage.created_at <= instante + PROPIO_DESPUES,
                )
            )
        )
        .scalars()
        .all()
        if marca_actual not in (m.content or "")
    ]
    elegido = elegir(instante, list(notificaciones), list(respuestas), agent_id, api_key_id, thread_user_id, propios)
    if elegido is None:
        return cita
    _, tipo, fila = elegido
    cita.tipo = tipo
    if tipo == "programada":
        cita.notification_id = str(fila.id)
        cita.task_id = str(fila.task_id or "")
        cita.titulo = " ".join(str(fila.title or "tarea programada").split())[:120]
        cita.texto = _texto(fila.title, fila.body)
    elif tipo == "propio":
        cita.texto = _texto_propio(fila.content)
    else:
        cita.task_id = str(fila.id)
        cita.texto = _texto(fila.response)
    return cita
