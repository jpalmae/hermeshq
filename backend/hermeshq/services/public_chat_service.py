import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.base import utcnow
from hermeshq.models.public_chat import (
    PublicChatApiKey,
    PublicChatMessage,
    PublicChatSession,
    PublicChatTranscript,
)
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.archivos_conversacion import CLAVE_SESION_ORIGEN, con_archivos_generados
from hermeshq.services.auto_background import AUTO_MARKER
from hermeshq.services.background_style import SIN_TEXTO_ENTRE_HERRAMIENTAS
from hermeshq.services.cancel_intent import (
    apunta_a_un_objeto,
    has_actionable_instruction,
    is_ack_request,
    is_cancel_request,
    is_reset_request,
    is_status_request,
    split_reset_prefix,
)
from hermeshq.services.channel_user_resolver import resolve_channel_user
from hermeshq.services.delegation_approval import approve_delegation_identity
from hermeshq.services.formato_pedido import DIRECTIVA_EXCEL, con_formato_pedido, directiva_formato
from hermeshq.services.respuesta_corta import pedido_original
from hermeshq.services.schedule_approval import (
    approval_code_from,
    approve_by_code,
    delete_by_code,
    deletion_code_from,
)
from hermeshq.services.schedule_notifications import metadata_de_entrega
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)

PUBLIC_CHAT_USER_ID = "__public_chat__"
PEDIDO_LARGO = re.compile(
    r"informes?\s+(interactivos?\s+)?(por\s+(rol|niveles)|niveles|interactivos?)|informe\s+interactivo"
    r"|\banálisis\s+profundo\b|\bcruce\b|\breprocesos?\b",
    re.IGNORECASE,
)
MENCION_EXCEL = re.compile(r"\bexcel\b|\.xlsx\b|\bplanillas?\b", re.IGNORECASE)
MOVER_ARCHIVO = re.compile(
    r"\b(s[uú]b[eai]\w*|env[ií]a\w*|m[aá]nd[ae]\w*|comp[aá]rt\w*|adj[uú]nt\w*|reenv[ií]a\w*|desc[aá]rg\w*|"
    r"mu[eé]ve\w*|c[oó]pia\w*|enlace|link|v[ií]nculo)\b",
    re.IGNORECASE,
)
TRABAJO_CON_DATOS = re.compile(
    r"\b(anali[sz]\w*|resum\w*|cruz\w*|compara\w*|compár[ae]\w*|proces\w*|consolid\w*|limpi\w*|calcul\w*|grafic\w*|"
    r"extrae\w*|totaliz\w*|agrupa\w*|tabla dinámica)\b",
    re.IGNORECASE,
)
AVISO_PEDIDO_LARGO = (
    "Este informe es largo: lo preparo en segundo plano y te aviso por aquí apenas esté listo. "
    "No hace falta reenviarlo; si prefieres detenerlo, escribe «cancela»."
)


REFERENCIA_A_UN_ARCHIVO = re.compile(
    r"adjunt|uploads/|\.(xlsx?|xlsm|csv)\b|sharepoint|onedrive|"
    r"\b(el|la|los|las|este|esta|ese|esa|del|de la|mi|tu) (excel|planillas?|archivos?|tablas?|hojas?)\b",
    re.IGNORECASE,
)
NUMERO = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w.,]*\w)")


def excel_con_los_datos_en_el_mensaje(texto: str) -> bool:
    return len(texto) <= 600 and len(NUMERO.findall(texto)) >= 6 and not REFERENCIA_A_UN_ARCHIVO.search(texto)


def es_pedido_largo(texto: str | None) -> bool:
    texto = texto or ""
    if PEDIDO_LARGO.search(texto):
        return True
    if not MENCION_EXCEL.search(texto) or excel_con_los_datos_en_el_mensaje(texto):
        return False
    if TRABAJO_CON_DATOS.search(texto):
        return True
    return directiva_formato(texto) == DIRECTIVA_EXCEL and not MOVER_ARCHIVO.search(texto)


RESPUESTA_CORTA_MAX = 24
MAX_MENSAJE_USUARIO_METADATA = 2000
ADJUNTO_CHAT = "archivos adjuntos"
ARCHIVO_CON_NOMBRE = re.compile(r"([\w][\w\-. ]{1,120}\.(?:pdf|xlsx|xlsm|csv|docx|pptx))", re.IGNORECASE)


async def buscar_en_sharepoint(db, vault, user_id: str, consulta: str) -> list[dict]:
    import asyncio
    import json as _json
    import urllib.request

    from hermeshq.services.m365_oauth import get_valid_token

    token, _, _ = await get_valid_token(user_id, vault, db)
    if not token:
        return []
    payload = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {"queryString": consulta},
                "from": 0,
                "size": 5,
                "fields": ["id", "name", "webUrl"],
            }
        ]
    }

    def _llamar() -> dict:
        req = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/search/query",
            data=_json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    data = await asyncio.to_thread(_llamar)
    hits = []
    for bloque in data.get("value", []):
        for contenedor in bloque.get("hitsContainers", []):
            for hit in contenedor.get("hits", []):
                recurso = hit.get("resource", {})
                hits.append({"name": recurso.get("name"), "url": recurso.get("webUrl")})
    return hits


REFERENCIA_EXPLICITA = re.compile(r"\b(lo|la|eso|esto|ese|esa)\b")


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


async def _prompt_con_referencia(db, agent_id: str, thread_user_id: str, content: str, session_id: str = "") -> str:
    texto = (content or "").strip()
    if not texto or len(texto) > RESPUESTA_CORTA_MAX or texto.startswith("/"):
        return content
    if has_actionable_instruction(texto) and not REFERENCIA_EXPLICITA.search(_sin_tildes(texto).lower()):
        return content
    result = await db.execute(
        select(Task)
        .where(Task.agent_id == agent_id, Task.status == "completed")
        .order_by(Task.queued_at.desc())
        .limit(10)
    )
    for previa in result.scalars().all():
        md = previa.metadata_json or {}
        if md.get("thread_user_id") != thread_user_id:
            continue
        if md.get("reset") or md.get("auto_reset"):
            break
        if md.get("background_job") or md.get("scheduled") or not previa.response:
            continue
        if session_id and md.get("public_session_id") != session_id:
            break
        if previa.response.lstrip().startswith("Este informe es largo"):
            continue
        referencia = " ".join(previa.response.split())[:400].replace("[", "(").replace("]", ")")
        return f"[Respuesta corta a tu último mensaje: «{referencia}». Interpreta «{texto}» como respuesta a eso, no a temas anteriores.]\n{content}"
    return content


MAX_TURNOS_DESDE_LISTADO = 4
MAX_TAREAS_LISTADO = 40


async def _referencia_listado_chat(
    db, agent_id: str, thread_user_id: str, session_id: str, api_key_id, content: str, cita=None
) -> dict | None:
    from hermeshq.models.public_chat import PublicChatNotification
    from hermeshq.services import referencias_correo as rc

    if cita is not None and (not cita.resuelta or cita.tipo == "programada" or not cita.task_id):
        return None
    if not rc.criterios_de_referencia(content or ""):
        return None
    clave = str(api_key_id or "")
    result = await db.execute(
        select(Task)
        .where(Task.agent_id == agent_id, Task.queued_at >= utcnow() - rc.VENTANA_IMPLICITA)
        .order_by(Task.queued_at.desc())
        .limit(MAX_TAREAS_LISTADO)
    )
    turnos = 0
    listado = None
    for previa in result.scalars().all():
        md = getattr(previa, "metadata_json", None) or {}
        if str(md.get("thread_user_id") or "") != thread_user_id:
            continue
        if md.get("background_job") or md.get("scheduled"):
            continue
        misma_clave = str(md.get("public_chat_api_key_id") or "") == clave
        if (md.get("reset") or md.get("auto_reset")) and (md.get("public_session_id") == session_id or misma_clave):
            break
        if md.get("source") != "public_chat" or md.get("public_session_id") != session_id or not misma_clave:
            continue
        if not md.get("conversation") or getattr(previa, "status", None) != "completed":
            continue
        turnos += 1
        if cita is not None:
            if str(previa.id) == str(cita.task_id):
                listado = previa if md.get(rc.CLAVE_LISTADO_CHAT) else None
                break
            continue
        if md.get(rc.CLAVE_LISTADO_CHAT):
            listado = previa
            break
        if turnos >= MAX_TURNOS_DESDE_LISTADO:
            return None
    if listado is None:
        return None
    elementos = (listado.metadata_json or {})[rc.CLAVE_LISTADO_CHAT].get("elementos") or []
    if cita is None:
        mostrado = getattr(listado, "completed_at", None) or listado.queued_at
        filtros = [
            PublicChatNotification.agent_id == agent_id,
            PublicChatNotification.recipient_user_id == thread_user_id,
            PublicChatNotification.schedule_id.isnot(None),
            PublicChatNotification.status == "delivered",
            PublicChatNotification.delivered_at > mostrado,
        ]
        if clave:
            filtros.append(PublicChatNotification.api_key_id == clave)
        posteriores = await db.execute(select(func.count()).select_from(PublicChatNotification).where(*filtros))
        if (posteriores.scalar() or 0) > 0:
            return None
    referencia = rc.referencia_en_listado(elementos, content)
    if not referencia:
        return None
    return {**referencia, "listado_task_id": str(listado.id)}


SENDER_PLATFORM = "microsoft_teams"
MAX_ACTIVE_SESSIONS_PER_KEY = 50
RACHA_FALLOS_MINIMA = 3
RACHA_FALLOS_VENTANA = 4
RACHA_HORAS = 4
RACHA_ERRORES = ("exceeded the task timeout", "produced no output for", "no result payload", "stopped early")
RESPUESTA_RAPIDA_MINUTOS = 60
EXITO_REAL_SEGUNDOS = 90
EXITO_REAL_ITERACIONES = 4
MAX_ASSISTANT_MESSAGE_LENGTH = 32_768
VENTANA_HERENCIA_MINUTOS = 55


class BindingError(ValueError):
    pass


def _has_widget_config(api_key: PublicChatApiKey) -> bool:
    return bool(
        api_key.widget_title
        or api_key.widget_theme != "auto"
        or api_key.widget_accent != "#6366f1"
        or api_key.widget_position != "right"
    )


async def _binding_problem(db: AsyncSession, agent_id: str, user_id: str) -> str | None:
    user = await db.get(User, user_id)
    if not user:
        return "Bound user not found"
    if not user.is_active:
        return "Bound user is inactive"
    result = await db.execute(
        select(AgentAssignment.id).where(
            AgentAssignment.agent_id == agent_id,
            AgentAssignment.user_id == user_id,
        )
    )
    if result.first() is None:
        return "Bound user is not assigned to this agent"
    return None


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _origin_allowed(origin: str | None, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return False
    if "*" in allowed_domains:
        return True
    if not origin:
        return False
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


class SimpleRateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)
        self.max_requests = max_requests
        self.window = window_seconds
        self._last_cleanup = time.time()
        self._cleanup_interval = 300

    def check(self, key: str) -> bool:
        now = time.time()
        self._requests[key] = [t for t in self._requests[key] if now - t < self.window]
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup(now)
        return True

    def _cleanup(self, now: float) -> None:
        empty_keys = [k for k, v in self._requests.items() if not v or all(now - t >= self.window for t in v)]
        for k in empty_keys:
            del self._requests[k]
        self._last_cleanup = now

    def excedido(self, key: str) -> bool:
        if key not in self._requests:
            return False
        now = time.time()
        self._requests[key] = [t for t in self._requests[key] if now - t < self.window]
        return len(self._requests[key]) >= self.max_requests


def consumir_cupos(cupos: list[tuple[object, str]]) -> bool:
    if any(limiter.excedido(clave) for limiter, clave in cupos if isinstance(limiter, SimpleRateLimiter)):
        return False
    return all([limiter.check(clave) for limiter, clave in cupos])


def ambito_de_bot(api_key) -> str:
    if getattr(api_key, "bound_user_id", None) or getattr(api_key, "trust_sender_id", False):
        return f"canal:{getattr(api_key, 'bot_channel_id', None) or 'predeterminado'}"
    return f"clave:{api_key.id}"


def clave_de_cuota(api_key, *, remitente: str | None, usuario_resuelto: str | None) -> str | None:
    if api_key is None:
        return None
    ambito = ambito_de_bot(api_key)
    if getattr(api_key, "bound_user_id", None):
        return f"{ambito}|usuario:{api_key.bound_user_id}"
    remitente = (remitente or "").strip()
    if getattr(api_key, "trust_sender_id", False) and remitente:
        if usuario_resuelto and usuario_resuelto != PUBLIC_CHAT_USER_ID:
            return f"{ambito}|usuario:{usuario_resuelto}"
        return f"{ambito}|remitente:{remitente}"
    return None


async def _epoca_actual(db, session_id: str) -> int:
    resultado = await db.execute(
        select(func.count(Task.id)).where(
            Task.prompt == "/reset",
            Task.metadata_json["public_session_id"].as_string() == session_id,
        )
    )
    return int(resultado.scalar() or 0)


LIMITES_POR_MINUTO = {
    "rate_limiter": "public_chat_session_messages_per_minute",
    "ip_rate_limiter": "public_chat_ip_messages_per_minute",
    "session_create_limiter": "public_chat_ip_sessions_per_minute",
    "user_rate_limiter": "public_chat_user_messages_per_minute",
    "user_session_limiter": "public_chat_user_sessions_per_minute",
    "bot_rate_limiter": "public_chat_bot_messages_per_minute",
    "bot_session_limiter": "public_chat_bot_sessions_per_minute",
    "auth_failure_limiter": "public_chat_ip_auth_failures_per_minute",
}


class PublicChatService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        supervisor: object,
        event_broker: object,
        secret_vault: object | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.supervisor = supervisor
        self.event_broker = event_broker
        for nombre in LIMITES_POR_MINUTO:
            self._limitador(nombre)
        self.notification_limiter = SimpleRateLimiter(max_requests=120, window_seconds=60)
        self._purge_task: asyncio.Task | None = None

    def _limitador(self, nombre: str):
        limitador = self.__dict__.get(nombre)
        if limitador is None:
            limitador = SimpleRateLimiter(getattr(get_settings(), LIMITES_POR_MINUTO[nombre]), 60)
            setattr(self, nombre, limitador)
        return limitador

    def frenar_ip_con_fallos(self, client_ip: str) -> None:
        limitador = self._limitador("auth_failure_limiter")
        if client_ip and isinstance(limitador, SimpleRateLimiter) and limitador.excedido(client_ip):
            raise ValueError("Rate limit exceeded")

    def anotar_fallo_de_autenticacion(self, client_ip: str) -> None:
        if client_ip:
            self._limitador("auth_failure_limiter").check(client_ip)

    def cupos_de_mensaje(
        self, api_key, session, session_id: str, thread_user_id: str, client_ip: str
    ) -> list[tuple[object, str]]:
        identidad = clave_de_cuota(
            api_key, remitente=getattr(session, "sender_id", None), usuario_resuelto=thread_user_id
        )
        cupos: list[tuple[object, str]] = []
        if api_key is not None:
            cupos.append((self._limitador("bot_rate_limiter"), ambito_de_bot(api_key)))
        if identidad is not None:
            cupos.append((self._limitador("user_rate_limiter"), identidad))
            return cupos
        cupos.append((self._limitador("rate_limiter"), session_id))
        if client_ip:
            cupos.append((self._limitador("ip_rate_limiter"), client_ip))
        return cupos

    def cupos_de_sesion(
        self, api_key, remitente: str | None, usuario_resuelto: str | None, client_ip: str
    ) -> list[tuple[object, str]]:
        identidad = clave_de_cuota(api_key, remitente=remitente, usuario_resuelto=usuario_resuelto)
        cupos: list[tuple[object, str]] = [(self._limitador("bot_session_limiter"), ambito_de_bot(api_key))]
        if identidad is not None:
            cupos.append((self._limitador("user_session_limiter"), identidad))
        elif client_ip:
            cupos.append((self._limitador("session_create_limiter"), client_ip))
        return cupos

    async def validate_api_key(
        self, raw_key: str, db: AsyncSession, *, origin: str | None = None, client_ip: str = ""
    ) -> PublicChatApiKey:
        self.frenar_ip_con_fallos(client_ip)
        try:
            return await self._clave_valida(raw_key, db, origin=origin)
        except ValueError:
            self.anotar_fallo_de_autenticacion(client_ip)
            raise

    async def _clave_valida(self, raw_key: str, db: AsyncSession, *, origin: str | None = None) -> PublicChatApiKey:
        key_hash = _hash_key(raw_key)
        result = await db.execute(
            select(PublicChatApiKey).where(
                PublicChatApiKey.key_hash == key_hash,
                PublicChatApiKey.is_active.is_(True),
            )
        )
        api_key = result.scalar_one_or_none()
        if not api_key:
            raise ValueError("Invalid or inactive API key")
        if not _origin_allowed(origin, api_key.allowed_domains):
            raise ValueError("Origin not allowed")
        return api_key

    async def create_session(
        self,
        api_key: PublicChatApiKey,
        db: AsyncSession,
        *,
        client_ip: str = "",
        sender_id: str | None = None,
        aad_object_id: str | None = None,
    ) -> dict:
        aad = (aad_object_id or "").strip() or None
        remitente = (sender_id or "").strip() or None if api_key.trust_sender_id else None
        usuario_resuelto = None
        if remitente:
            usuario_resuelto, _ = await self._resolve_identity(
                api_key,
                SimpleNamespace(agent_id=api_key.agent_id, sender_id=remitente, aad_object_id=aad),
                db,
            )
        if not consumir_cupos(self.cupos_de_sesion(api_key, remitente, usuario_resuelto, client_ip)):
            raise ValueError("Rate limit exceeded")
        if aad and api_key.bound_user_id and await self._aad_ajeno_a_la_clave(api_key, aad, db):
            raise ValueError("Sender is not the bound user")

        active_count_result = await db.execute(
            select(func.count()).where(
                PublicChatSession.api_key_id == api_key.id,
                PublicChatSession.status == "active",
            )
        )
        if (active_count_result.scalar() or 0) >= MAX_ACTIVE_SESSIONS_PER_KEY:
            raise ValueError("Too many active sessions")

        agent_id = api_key.agent_id
        agent = await db.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")

        raw_session_token = secrets.token_urlsafe(48)
        session_token_hash = _hash_key(raw_session_token)
        previa = await self._sesion_a_heredar(api_key, db, sender_id)
        if previa is not None:
            previa.session_token = session_token_hash
            previa.status = "active"
            previa.last_activity = utcnow()
            if aad:
                previa.aad_object_id = aad
            await db.commit()
            logger.info("Sesión pública %s retomada tras caducar (misma conversación)", previa.id)
            return {
                "session_id": previa.id,
                "session_token": raw_session_token,
                "agent_name": agent.friendly_name or agent.name,
            }
        chat_session = PublicChatSession(
            api_key_id=api_key.id,
            agent_id=agent_id,
            session_token=session_token_hash,
            ttl_minutes=10,
            sender_id=(sender_id or "").strip() or None if api_key.trust_sender_id else None,
            aad_object_id=aad,
        )
        identidad, _ = await self._resolve_identity(api_key, chat_session, db)
        if identidad != PUBLIC_CHAT_USER_ID:
            chat_session.user_id = identidad
        db.add(chat_session)
        await db.commit()
        await db.refresh(chat_session)

        return {
            "session_id": chat_session.id,
            "session_token": raw_session_token,
            "agent_name": agent.friendly_name or agent.name,
        }

    async def _aad_ajeno_a_la_clave(self, api_key: PublicChatApiKey, aad: str, db: AsyncSession) -> bool:
        from hermeshq.services.canales_bot import id_canal_efectivo, identidad_en_canal, identidades_de_usuario

        canal_id, _ = await id_canal_efectivo(db, api_key)
        identidad = identidad_en_canal(await identidades_de_usuario(db, api_key.bound_user_id), canal_id)
        registrado = (getattr(identidad, "aad_object_id", None) or "").strip()
        return bool(registrado and registrado != aad)

    async def _sesion_a_heredar(
        self, api_key: PublicChatApiKey, db: AsyncSession, sender_id: str | None
    ) -> PublicChatSession | None:
        if api_key.bound_user_id:
            remitente = None
        elif api_key.trust_sender_id and (sender_id or "").strip():
            remitente = sender_id.strip()
        else:
            return None
        identidad, _ = await self._resolve_identity(
            api_key, SimpleNamespace(agent_id=api_key.agent_id, sender_id=remitente), db
        )
        if identidad == PUBLIC_CHAT_USER_ID:
            return None
        ahora = utcnow()
        consulta = select(PublicChatSession).where(
            PublicChatSession.api_key_id == api_key.id,
            PublicChatSession.agent_id == api_key.agent_id,
            PublicChatSession.last_activity >= ahora - timedelta(minutes=VENTANA_HERENCIA_MINUTOS),
        )
        if remitente is not None:
            consulta = consulta.where(PublicChatSession.sender_id == remitente)
        resultado = await db.execute(consulta.order_by(PublicChatSession.last_activity.desc()).limit(1))
        previa = resultado.scalar_one_or_none()
        if previa is None or previa.status not in ("active", "expired"):
            return None
        if previa.status == "active" and (ahora - previa.last_activity).total_seconds() <= previa.ttl_minutes * 60:
            return None
        ultima = await db.execute(
            select(Task)
            .where(
                Task.agent_id == api_key.agent_id,
                Task.metadata_json["public_session_id"].as_string() == previa.id,
            )
            .order_by(Task.queued_at.desc())
            .limit(1)
        )
        tarea = ultima.scalar_one_or_none()
        if tarea is None:
            return None
        metadata = tarea.metadata_json or {}
        if metadata.get("reset") or metadata.get("thread_user_id") != identidad:
            return None
        return previa

    async def renovar_actividad(self, session_id: str, db: AsyncSession | None = None) -> None:
        if not session_id:
            return
        if db is None:
            async with self.session_factory() as propia:
                await self.renovar_actividad(session_id, propia)
                await propia.commit()
            return
        sesion = await db.get(PublicChatSession, session_id)
        if sesion is not None and sesion.status == "active":
            sesion.last_activity = utcnow()

    async def _resolve_identity(
        self,
        api_key: PublicChatApiKey | None,
        session: PublicChatSession,
        db: AsyncSession,
    ) -> tuple[str, str | None]:
        """Return (thread_user_id, denied_reason). Falls back to anonymous on any failure."""
        if not api_key:
            return PUBLIC_CHAT_USER_ID, None

        candidate: str | None = None
        denied: str | None = None
        if api_key.bound_user_id:
            candidate = api_key.bound_user_id
        elif api_key.trust_sender_id and session.sender_id:
            user = await resolve_channel_user(
                db,
                SENDER_PLATFORM,
                session.sender_id,
                bot_channel_id=getattr(api_key, "bot_channel_id", None),
                aad_object_id=getattr(session, "aad_object_id", None),
            )
            if user:
                candidate = user.id
            else:
                denied = "Sender is not linked to any user"
        else:
            return PUBLIC_CHAT_USER_ID, None

        if candidate:
            denied = await _binding_problem(db, session.agent_id, candidate)
            if denied is None and not api_key.is_active:
                denied = "API key is inactive"
        if denied or not candidate:
            logger.warning("Public chat key %s degraded to anonymous: %s", api_key.id, denied)
            return PUBLIC_CHAT_USER_ID, denied
        return candidate, None

    @staticmethod
    def sesion_ajena(api_key: PublicChatApiKey | None, session: PublicChatSession, thread_user_id: str) -> bool:
        if isinstance(api_key, PublicChatApiKey) and not api_key.is_active:
            return True
        propietario = getattr(session, "user_id", None)
        return isinstance(propietario, str) and bool(propietario) and propietario != thread_user_id

    async def _cortar_sesion_ajena(self, db: AsyncSession, api_key, session, thread_user_id: str) -> None:
        if not self.sesion_ajena(api_key, session, thread_user_id):
            return
        logger.warning(
            "Sesión pública %s cortada: clave %s inactiva o usuario %s distinto del que la abrió",
            session.id,
            getattr(api_key, "id", None),
            thread_user_id,
        )
        session.status = "expired"
        await db.commit()
        raise ValueError("Session expired")

    async def validate_session(
        self, session_id: str, session_token: str, db: AsyncSession, *, client_ip: str = ""
    ) -> PublicChatSession:
        self.frenar_ip_con_fallos(client_ip)
        token_hash = _hash_key(session_token)
        result = await db.execute(
            select(PublicChatSession).where(
                PublicChatSession.id == session_id,
                PublicChatSession.session_token == token_hash,
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            self.anotar_fallo_de_autenticacion(client_ip)
            raise ValueError("Session not found or inactive")
        if session.status != "active":
            raise ValueError("Session not found or inactive")

        now = datetime.now(UTC)
        elapsed = (now - session.last_activity).total_seconds()
        if elapsed > session.ttl_minutes * 60:
            session.status = "expired"
            await db.commit()
            raise ValueError("Session expired")

        return session

    async def _check_monthly_quota(
        self,
        api_key_id: str,
        requests_per_month: int,
        tokens_per_month: int,
        db: AsyncSession,
    ) -> None:
        first_of_month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        session_ids_subq = select(PublicChatSession.id).where(
            PublicChatSession.api_key_id == api_key_id,
            PublicChatSession.created_at >= first_of_month,
        )

        result = await db.execute(
            select(func.count()).where(
                PublicChatMessage.role == "user",
                PublicChatMessage.session_id.in_(session_ids_subq),
            )
        )
        count = result.scalar() or 0
        if count >= requests_per_month:
            raise ValueError("Monthly request quota exceeded")

        token_result = await db.execute(
            select(func.coalesce(func.sum(Task.tokens_used), 0)).where(
                Task.metadata_json["source"].as_string() == "public_chat",
                Task.metadata_json["public_session_id"].as_string().in_(session_ids_subq),
                Task.queued_at >= first_of_month,
            )
        )
        tokens = token_result.scalar() or 0
        if tokens >= tokens_per_month:
            raise ValueError("Monthly token quota exceeded")

    async def _con_busqueda_previa(self, db, thread_user_id: str, content: str, prompt_tarea: str) -> str:
        vault = getattr(self, "secret_vault", None)
        if not vault or ADJUNTO_CHAT in _sin_tildes(content).lower():
            return prompt_tarea
        nombres = [m.group(1).strip() for m in ARCHIVO_CON_NOMBRE.finditer(content or "")]
        if not nombres:
            return prompt_tarea
        consulta = nombres[0]
        try:
            hits = await buscar_en_sharepoint(db, vault, thread_user_id, consulta)
        except Exception:  # noqa: BLE001
            logger.exception("Búsqueda previa en SharePoint falló para %r", consulta)
            return prompt_tarea + f"\n[Búsqueda en SharePoint por «{consulta}» no disponible en este momento.]"
        if not hits:
            return prompt_tarea + f"\n[Búsqueda en SharePoint por «{consulta}»: no hay archivos con ese nombre.]"
        lista = "; ".join(f"{h['name']} ({h['url']})" for h in hits[:5])
        return prompt_tarea + f"\n[Búsqueda en SharePoint por «{consulta}»: {lista}]"

    @staticmethod
    def _task_belongs_to_chat(task: Task, session_id: str, thread_user_id: str, api_key_id: str | None = None) -> bool:
        metadata = task.metadata_json or {}
        clave_de_la_tarea = metadata.get("public_chat_api_key_id")
        if api_key_id and clave_de_la_tarea and str(clave_de_la_tarea) != str(api_key_id):
            return False
        if metadata.get("background_job"):
            return thread_user_id != PUBLIC_CHAT_USER_ID and metadata.get("thread_user_id") == thread_user_id
        if metadata.get("scheduled"):
            return (
                bool(metadata.get("manual_trigger") and metadata.get("solicitado_desde_chat"))
                and thread_user_id != PUBLIC_CHAT_USER_ID
                and metadata.get("thread_user_id") == thread_user_id
            )
        if metadata.get("source") != "public_chat":
            return False
        if metadata.get("public_session_id") == session_id:
            return True
        return thread_user_id != PUBLIC_CHAT_USER_ID and metadata.get("thread_user_id") == thread_user_id

    @staticmethod
    def _reset_marker(session, session_id: str, thread_user_id: str, automatico: bool = False, epoca: int = 0) -> Task:
        clave = getattr(session, "api_key_id", None)
        task = Task(
            agent_id=session.agent_id,
            created_by_user_id=None,
            title="Reinicio automático de conversación" if automatico else "Reinicio de conversación",
            prompt="/reset",
            status="completed",
            response="Conversación reiniciada",
            completed_at=utcnow(),
            metadata_json={
                "conversation": True,
                "reset": True,
                "conversation_epoch": int(epoca or 0),
                **({"auto_reset": True} if automatico else {}),
                "source": "public_chat",
                "public_session_id": session_id,
                "thread_user_id": thread_user_id,
                **({"public_chat_api_key_id": str(clave)} if isinstance(clave, str) and clave else {}),
            },
        )
        task.board_column = runtime_status_to_board_column("completed")
        task.board_order = next_board_order()
        task.board_manual = False
        return task

    @staticmethod
    def calcular_racha(tareas: list, ahora=None) -> dict:
        """Fallos por tiempo agotado de este chat (más recientes primero), desde el último reinicio y dentro de RACHA_HORAS.

        consecutivos: seguidos desde la última tarea que sí avanzó; ventana: todos los de la ventana, aunque haya habido avances.
        """
        ahora = ahora or utcnow()
        consecutivos, ventana, segundos, cortada = 0, 0, 0.0, False
        for tarea in tareas:
            metadata = getattr(tarea, "metadata_json", None) or {}
            if metadata.get("reset"):
                break
            if AUTO_MARKER in metadata:
                continue
            encolada = getattr(tarea, "queued_at", None)
            if encolada is not None and ahora - encolada > timedelta(hours=RACHA_HORAS):
                break
            inicio, fin = getattr(tarea, "started_at", None), getattr(tarea, "completed_at", None)
            duracion = (fin - inicio).total_seconds() if inicio and fin else 0.0
            estado = getattr(tarea, "status", "")
            if estado == "failed" and any(m in (getattr(tarea, "error_message", "") or "") for m in RACHA_ERRORES):
                ventana += 1
                segundos += duracion
                if not cortada:
                    consecutivos += 1
            elif estado == "completed" and (
                duracion >= EXITO_REAL_SEGUNDOS or (getattr(tarea, "iterations", 0) or 0) >= EXITO_REAL_ITERACIONES
            ):
                cortada = True
        return {"consecutivos": consecutivos, "ventana": ventana, "minutos": round(segundos / 60)}

    @staticmethod
    def debe_reiniciar(racha: dict) -> bool:
        return racha["consecutivos"] >= RACHA_FALLOS_MINIMA or racha["ventana"] >= RACHA_FALLOS_VENTANA

    async def _racha_del_chat(self, db, session, session_id: str, thread_user_id: str) -> dict:
        resultado = await db.execute(
            select(Task).where(Task.agent_id == session.agent_id).order_by(Task.queued_at.desc()).limit(25)
        )
        propias = [
            t
            for t in resultado.scalars().all()
            if self._task_belongs_to_chat(t, session_id, thread_user_id, getattr(session, "api_key_id", None))
        ]
        propias = [
            t for t in propias if (t.metadata_json or {}).get("conversation") or (t.metadata_json or {}).get("reset")
        ]
        return self.calcular_racha(propias)

    @staticmethod
    def _status_reply(tasks: list) -> str:
        ahora = utcnow()
        corriendo = [t for t in tasks if t.status == "running"]
        orden = sorted(
            corriendo or tasks, key=lambda t: bool((getattr(t, "metadata_json", None) or {}).get("background_job"))
        )
        principal = orden[0]
        inicio = principal.started_at or principal.queued_at
        segundos = max(0, int((ahora - inicio).total_seconds())) if inicio else 0
        tiempo = f"{segundos // 60} min {segundos % 60:02d} s" if segundos >= 60 else f"{segundos} s"
        metadata = getattr(principal, "metadata_json", None) or {}
        if metadata.get("background_job"):
            titulo = metadata.get("background_title") or "tu análisis"
            return (
                f"⏳ Tu trabajo en segundo plano «{titulo}» sigue en curso (lleva {tiempo}). "
                "Te aviso por aquí apenas termine; mientras tanto puedes seguir pidiéndome otras cosas. "
                "Si prefieres detenerlo, escribe «cancela»."
            )
        pedido = " ".join((principal.prompt or "").split())
        pedido = pedido[:90] + "…" if len(pedido) > 90 else pedido
        if corriendo:
            estado = f"⏳ Sigo trabajando en tu solicitud (llevo {tiempo}): «{pedido}»."
        else:
            estado = f"⏳ Tu solicitud está en cola y empieza en breve: «{pedido}»."
        extra = f" Tienes {len(tasks) - 1} mensaje(s) más esperando." if len(tasks) > 1 else ""
        return (
            estado
            + extra
            + " Te respondo apenas termine; no hace falta reenviarla. Si prefieres detenerla, escribe «cancela»."
        )

    async def _failed_attempt_reply(self, db, session, session_id: str, thread_user_id: str) -> str | None:
        result = await db.execute(
            select(Task).where(Task.agent_id == session.agent_id).order_by(Task.queued_at.desc()).limit(25)
        )
        propias = [
            t
            for t in result.scalars().all()
            if self._task_belongs_to_chat(t, session_id, thread_user_id, getattr(session, "api_key_id", None))
        ]
        propias = [t for t in propias if (t.metadata_json or {}).get("conversation")]
        if not propias:
            return None
        ultima = propias[0]
        reciente = ultima.completed_at and utcnow() - ultima.completed_at <= timedelta(minutes=RESPUESTA_RAPIDA_MINUTOS)
        if ultima.status != "failed" or not reciente or "exceeded the task timeout" not in (ultima.error_message or ""):
            return None
        pedido = " ".join(pedido_original(ultima.prompt).split())
        pedido = pedido[:90] + "…" if len(pedido) > 90 else pedido
        return (
            f"⚠️ Mi intento anterior se agotó antes de terminar tu solicitud «{pedido}». "
            "Escribe «hazlo en segundo plano» y la retomo aparte, con hasta 60 minutos, y te aviso por aquí con el resultado."
        )

    async def _cancelar_tareas(self, tareas: list) -> int:
        active = getattr(self.supervisor, "active_tasks", {})
        for task in tareas:
            if task.id in active:
                await self.supervisor.cancel_task(task.id)
            else:
                task.status = "cancelled"
                task.completed_at = utcnow()
                task.board_column = runtime_status_to_board_column("cancelled")
        return len(tareas)

    async def _running_interactive_task(
        self, db: AsyncSession, session, session_id: str, thread_user_id: str
    ) -> Task | None:
        result = await db.execute(select(Task).where(Task.agent_id == session.agent_id, Task.status == "running"))
        for task in result.scalars().all():
            if task.status != "running":
                continue
            metadata = task.metadata_json or {}
            if metadata.get("background_job") or metadata.get("scheduled"):
                continue
            if self._task_belongs_to_chat(task, session_id, thread_user_id, getattr(session, "api_key_id", None)):
                return task
        return None

    @staticmethod
    def _inflight_followup_reply(task: Task) -> str:
        ahora = utcnow()
        inicio = task.started_at or task.queued_at
        segundos = max(0, int((ahora - inicio).total_seconds())) if inicio else 0
        tiempo = f"{segundos // 60} min {segundos % 60:02d} s" if segundos >= 60 else f"{segundos} s"
        pedido = " ".join((task.prompt or "").split())
        pedido = pedido[:90] + "…" if len(pedido) > 90 else pedido
        return (
            f"⏳ Sigo trabajando en tu solicitud anterior (llevo {tiempo}): «{pedido}». "
            "No alcancé a procesar este mensaje nuevo; reenvíalo cuando te haya respondido. "
            "Si prefieres detener lo anterior, escribe «cancela»."
        )

    async def handle_cancel_request(
        self,
        session_id: str,
        session_token: str,
        content: str,
        db: AsyncSession,
        *,
        client_ip: str = "",
    ) -> str | None:
        """If the message only asks to stop (or to reset), cancel this chat's running tasks and return the reply text."""
        approval_code = approval_code_from(content)
        delete_code = deletion_code_from(content)
        texto_tras_reset = split_reset_prefix(content)
        reset = is_reset_request(content) or bool(texto_tras_reset)
        status = not reset and not is_cancel_request(content) and is_status_request(content)
        ack = not reset and not status and not is_cancel_request(content) and is_ack_request(content)
        if (
            not reset
            and not status
            and not ack
            and not approval_code
            and not delete_code
            and not is_cancel_request(content)
        ):
            return None
        session = await self.validate_session(session_id, session_token, db, client_ip=client_ip)
        api_key = await db.get(PublicChatApiKey, session.api_key_id)
        thread_user_id, _ = await self._resolve_identity(api_key, session, db)
        await self._cortar_sesion_ajena(db, api_key, session, thread_user_id)
        if not consumir_cupos(self.cupos_de_mensaje(api_key, session, session_id, thread_user_id, client_ip)):
            raise ValueError("Rate limit exceeded")
        session.last_activity = utcnow()
        result = await db.execute(
            select(Task).where(Task.agent_id == session.agent_id, Task.status.in_(("running", "queued")))
        )
        mine = [
            task
            for task in result.scalars().all()
            if self._task_belongs_to_chat(task, session_id, thread_user_id, getattr(session, "api_key_id", None))
        ]
        if approval_code:
            if thread_user_id == PUBLIC_CHAT_USER_ID:
                return None
            reply = await approve_by_code(db, session.agent_id, thread_user_id, approval_code, "public_chat")
            nueva_tarea_id = None
            if reply is None:
                nueva_tarea_id, reply = await approve_delegation_identity(
                    db, session.agent_id, thread_user_id, approval_code
                )
                if reply is None:
                    return None
            db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
            db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
            await db.commit()
            if nueva_tarea_id:
                await self.supervisor.submit_task(nueva_tarea_id)
            return reply
        if delete_code:
            if thread_user_id == PUBLIC_CHAT_USER_ID:
                return None
            reply = await delete_by_code(db, session.agent_id, thread_user_id, delete_code, "public_chat")
            if reply is None:
                return None
            db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
            db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
            await db.commit()
            return reply
        if ack and not any((task.metadata_json or {}).get("background_job") for task in mine):
            return None
        if status or ack:
            if not mine:
                reply = await self._failed_attempt_reply(db, session, session_id, thread_user_id)
                if not reply:
                    return None
                db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
                db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
                await db.commit()
                return reply
            reply = self._status_reply(mine)
            db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
            db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
            await db.commit()
            return reply
        if reset:
            mine = [task for task in mine if not (task.metadata_json or {}).get("scheduled")]
        elif not mine and apunta_a_un_objeto(content):
            return None
        canceladas = await self._cancelar_tareas(mine)
        if texto_tras_reset:
            await db.commit()
            return None
        if reset:
            db.add(
                self._reset_marker(session, session_id, thread_user_id, epoca=await _epoca_actual(db, session_id) + 1)
            )
            reply = (
                "🧹 Listo, conversación reiniciada: empezamos de cero. ¿Qué necesitas?"
                if not canceladas
                else f"🧹 Listo, conversación reiniciada: empezamos de cero. Cancelé {canceladas} "
                f"{'tarea' if canceladas == 1 else 'tareas'} en curso. ¿Qué necesitas?"
            )
        elif mine:
            reply = (
                "⏹️ Listo, cancelé la tarea en curso. ¿Qué necesitas ahora?"
                if len(mine) == 1
                else f"⏹️ Listo, cancelé las {len(mine)} tareas en curso. ¿Qué necesitas ahora?"
            )
        else:
            reply = "No tengo ninguna tarea en curso para cancelar. ¿Qué necesitas?"
        db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
        db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
        await db.commit()
        return reply

    async def send_message(
        self,
        session_id: str,
        session_token: str,
        content: str,
        db: AsyncSession,
        *,
        client_ip: str = "",
    ) -> tuple[str | None, str]:
        session = await self.validate_session(session_id, session_token, db, client_ip=client_ip)

        api_key = await db.get(PublicChatApiKey, session.api_key_id)
        thread_user_id, binding_denied = await self._resolve_identity(api_key, session, db)
        await self._cortar_sesion_ajena(db, api_key, session, thread_user_id)
        if not consumir_cupos(self.cupos_de_mensaje(api_key, session, session_id, thread_user_id, client_ip)):
            raise ValueError("Rate limit exceeded")
        if api_key:
            await self._check_monthly_quota(
                api_key.id,
                api_key.requests_per_month,
                api_key.tokens_per_month,
                db,
            )
        session.last_activity = utcnow()

        en_curso = await self._running_interactive_task(db, session, session_id, thread_user_id)
        if en_curso is not None and not split_reset_prefix(content):
            reply = self._inflight_followup_reply(en_curso)
            db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
            db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
            await db.commit()
            return None, reply

        resto = split_reset_prefix(content)
        if resto:
            db.add(
                self._reset_marker(session, session_id, thread_user_id, epoca=await _epoca_actual(db, session_id) + 1)
            )
            db.add(PublicChatMessage(session_id=session_id, role="user", content="/reset"))
            await db.flush()
            content = resto

        aviso_racha = None
        if not resto:
            racha = await self._racha_del_chat(db, session, session_id, thread_user_id)
            if self.debe_reiniciar(racha):
                db.add(
                    self._reset_marker(
                        session,
                        session_id,
                        thread_user_id,
                        automatico=True,
                        epoca=await _epoca_actual(db, session_id) + 1,
                    )
                )
                await db.flush()
                aviso_racha = {"fallos": racha["ventana"], "minutos": racha["minutos"]}

        user_msg = PublicChatMessage(
            session_id=session_id,
            role="user",
            content=content,
        )
        db.add(user_msg)

        agent = await db.get(Agent, session.agent_id)
        if not agent or agent.status != "running":
            raise ValueError("Agent is not available")

        task_metadata = {
            "conversation": True,
            "source": "public_chat",
            "public_session_id": session_id,
            "thread_user_id": thread_user_id,
            **metadata_de_entrega(
                {
                    "public_chat_api_key_id": session.api_key_id,
                    "bot_channel_id": getattr(api_key, "bot_channel_id", None),
                }
            ),
        }
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            from hermeshq.services.aprobacion_central import aprobar_texto_si_corresponde

            aprobada = await aprobar_texto_si_corresponde(
                db, session.agent_id, thread_user_id, session_id, content, "public_chat"
            )
            if aprobada:
                task_metadata["aprobacion_id"] = aprobada
            from hermeshq.services import ejecucion_aprobada

            aviso = await ejecucion_aprobada.avisos_pendientes(db, session.agent_id, thread_user_id, session_id)
            if aviso:
                task_metadata["aprobacion_aviso"] = aviso
        if aviso_racha:
            task_metadata["auto_reset_aviso"] = aviso_racha
        task_metadata["conversation_epoch"] = await _epoca_actual(db, session_id)
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            task_metadata["mensaje_usuario"] = (content or "")[:MAX_MENSAJE_USUARIO_METADATA]
            task_metadata["public_chat_bound_user_id"] = thread_user_id
            if api_key and not api_key.bound_user_id:
                task_metadata["public_chat_sender_id"] = session.sender_id
        elif binding_denied:
            task_metadata["public_chat_binding_denied"] = binding_denied

        cita = None
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            from hermeshq.services.cita_teams import resolver_cita

            cita = await resolver_cita(db, session.agent_id, session.api_key_id, thread_user_id, content)
        if cita is not None:
            content = cita.texto_usuario
            if cita.resuelta:
                task_metadata["cita"] = {
                    "tipo": cita.tipo,
                    "task_id": cita.task_id,
                    "notification_id": cita.notification_id,
                }
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            from hermeshq.services import adjunto_pendiente as ap

            if ap.es_solo_adjunto(content):
                retenido = await ap.pedido_retenido(db, session_id)
                if retenido:
                    content = ap.unir_con_retenido(content, retenido)
                    task_metadata["mensaje_usuario"] = content[:MAX_MENSAJE_USUARIO_METADATA]
                    task_metadata["adjunto_unido_a_pedido"] = True
            elif es_pedido_largo(content):
                objeto = ap.menciona_adjunto_ausente(content)
                if objeto:
                    reply = ap.aviso_falta_adjunto(objeto)
                    db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
                    await db.commit()
                    return None, reply
        plan_confirmado = None
        etapa_plan = ""
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            from hermeshq.services import plan_analisis as pa

            en_curso_analisis = await self._analisis_en_curso(db, session.agent_id, thread_user_id, session_id)
            if (
                en_curso_analisis is not None
                and pa.parece_correccion(content)
                and not es_pedido_largo(content)
                and getattr(agent, "workspace_path", None)
            ):
                pa.registrar_correccion(agent.workspace_path, en_curso_analisis.id, content)
                reply = pa.aviso_correccion_anotada(content)
                db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
                await db.commit()
                return None, reply
            pendiente = await pa.plan_pendiente(db, session.agent_id, session_id)
            if pendiente is not None and pa.responde_al_plan(content) and not pa.necesita_plan(content):
                original = str((pendiente.metadata_json or {}).get("pedido_original") or "")
                etapa = str((pendiente.metadata_json or {}).get("etapa_plan") or "plan")
                plan_confirmado = {
                    "plan_task_id": pendiente.id,
                    "pedido_original": original,
                    "contenido": pa.pedido_confirmado(original, pendiente.response or "", content, etapa),
                }
            elif es_pedido_largo(content) and not await self._mismo_trabajo_en_curso(
                db, session.agent_id, thread_user_id, content
            ):
                if pa.necesita_plan(content):
                    etapa_plan = "plan"
                elif pa.necesita_fuente(content):
                    etapa_plan = "fuente"
        if thread_user_id != PUBLIC_CHAT_USER_ID and (plan_confirmado or (es_pedido_largo(content) and not etapa_plan)):
            from hermeshq.services.auto_background import _history_text, _load_recent_messages, _title_from

            texto_formato = content
            if plan_confirmado:
                texto_formato = plan_confirmado["pedido_original"]
                content = plan_confirmado["contenido"]

            pedido_normalizado = _title_from(content)
            en_curso = await db.execute(
                select(Task).where(
                    Task.agent_id == session.agent_id,
                    Task.status.in_(("pending", "queued", "running")),
                )
            )
            for activa in en_curso.scalars().all():
                md_activa = activa.metadata_json or {}
                if not md_activa.get("background_job") or md_activa.get("thread_user_id") != thread_user_id:
                    continue
                if (activa.title or "").removeprefix("Trabajo en segundo plano: ") == pedido_normalizado:
                    reply = (
                        "Ya estoy trabajando en ese pedido; no lo encolo otra vez. "
                        "Te entrego el resultado en cuanto termine."
                    )
                    db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
                    db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
                    await db.commit()
                    return None, reply

            mensajes = await _load_recent_messages(db, session_id)
            if mensajes and mensajes[0].role == "user" and (mensajes[0].content or "") == content:
                mensajes = mensajes[1:]
            historial = _history_text(mensajes)
            aviso = Task(
                agent_id=session.agent_id,
                created_by_user_id=None,
                title="Public chat message",
                prompt=content,
                status="completed",
                response=AVISO_PEDIDO_LARGO,
                completed_at=utcnow(),
                metadata_json=task_metadata,
            )
            aviso.board_column = runtime_status_to_board_column("completed")
            aviso.board_order = next_board_order()
            aviso.board_manual = False
            contexto = f"Conversación reciente:\n{historial}\n\n" if historial else ""
            prefijo_cita = f"{cita.prefijo()}\n" if cita is not None else ""
            cuerpo = await con_archivos_generados(
                db,
                session.agent_id,
                thread_user_id,
                session_id,
                session.api_key_id,
                f"{contexto}{prefijo_cita}Pedido del usuario:\n{content.strip()}\n\n"
                "[TRABAJO EN SEGUNDO PLANO: el usuario no ve este trabajo ni puede responderte. "
                f"{SIN_TEXTO_ENTRE_HERRAMIENTAS} Entrega al final el resultado completo y listo para leer.]",
            )
            trabajo = Task(
                agent_id=session.agent_id,
                created_by_user_id=None,
                title=f"Trabajo en segundo plano: {pedido_normalizado}"[:255],
                prompt=con_formato_pedido(texto_formato, cuerpo),
                metadata_json={
                    "background_job": True,
                    "background_title": pedido_normalizado,
                    "mensaje_usuario": (content or "")[:MAX_MENSAJE_USUARIO_METADATA],
                    **(
                        {"plan_task_id": plan_confirmado["plan_task_id"], "analisis_con_plan": True}
                        if plan_confirmado
                        else {}
                    ),
                    "source": "background_job",
                    "thread_user_id": thread_user_id,
                    "pedido_largo": True,
                    CLAVE_SESION_ORIGEN: session_id,
                    **metadata_de_entrega(task_metadata),
                },
            )
            trabajo.board_column = runtime_status_to_board_column(trabajo.status)
            trabajo.board_order = next_board_order()
            trabajo.board_manual = False
            db.add(aviso)
            db.add(trabajo)
            await db.commit()
            await db.refresh(aviso)
            await self.supervisor.submit_task(trabajo.id)
            return None, AVISO_PEDIDO_LARGO

        prompt_tarea = content
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            if cita is None:
                prompt_tarea = await _prompt_con_referencia(db, session.agent_id, thread_user_id, content, session_id)
            try:
                referencia = await _referencia_listado_chat(
                    db, session.agent_id, thread_user_id, session_id, session.api_key_id, content, cita
                )
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo resolver la referencia a la lista de correos del chat %s", session_id)
                referencia = None
            if referencia:
                prompt_tarea = f"{referencia['linea']}\n{prompt_tarea}"
                if referencia["correos"]:
                    task_metadata["referencia_correo_chat"] = {
                        "listado_task_id": referencia["listado_task_id"],
                        "correos": referencia["correos"],
                    }
            prompt_tarea = await self._con_busqueda_previa(db, thread_user_id, content, prompt_tarea)
            prompt_tarea = await con_archivos_generados(
                db,
                session.agent_id,
                thread_user_id,
                session_id,
                session.api_key_id,
                prompt_tarea,
                aviso_conversacion_nueva=True,
            )
        prompt_tarea = con_formato_pedido(content, prompt_tarea)
        if cita is not None:
            prompt_tarea = f"{cita.prefijo()}\n{prompt_tarea}"
        if etapa_plan:
            prompt_tarea = pa.con_directiva_plan(prompt_tarea, etapa_plan)
            task_metadata["plan_analisis"] = True
            task_metadata["etapa_plan"] = etapa_plan
            task_metadata["pedido_original"] = (content or "")[:MAX_MENSAJE_USUARIO_METADATA]
        task = Task(
            agent_id=session.agent_id,
            created_by_user_id=None,
            title="Public chat message",
            prompt=prompt_tarea,
            metadata_json=task_metadata,
        )
        task.board_column = runtime_status_to_board_column(task.status)
        task.board_order = next_board_order()
        task.board_manual = False
        db.add(task)
        await db.commit()
        await db.refresh(task)

        await self.supervisor.submit_task(task.id)

        return task.id, session.agent_id

    async def _mismo_trabajo_en_curso(self, db: AsyncSession, agent_id: str, user_id: str, content: str) -> bool:
        from hermeshq.services.auto_background import _title_from

        titulo = _title_from(content)
        resultado = await db.execute(
            select(Task).where(Task.agent_id == agent_id, Task.status.in_(("pending", "queued", "running")))
        )
        return any(
            (t.metadata_json or {}).get("background_job")
            and (t.metadata_json or {}).get("thread_user_id") == user_id
            and (t.title or "").removeprefix("Trabajo en segundo plano: ") == titulo
            for t in resultado.scalars().all()
        )

    async def _analisis_en_curso(self, db: AsyncSession, agent_id: str, user_id: str, session_id: str):
        resultado = await db.execute(
            select(Task).where(Task.agent_id == agent_id, Task.status.in_(("pending", "queued", "running")))
        )
        for tarea in resultado.scalars().all():
            metadata = tarea.metadata_json or {}
            if (
                metadata.get("analisis_con_plan")
                and metadata.get("thread_user_id") == user_id
                and metadata.get(CLAVE_SESION_ORIGEN) == session_id
            ):
                return tarea
        return None

    async def save_assistant_message(self, session_id: str, content: str) -> None:
        truncated = content[:MAX_ASSISTANT_MESSAGE_LENGTH] if content else ""
        async with self.session_factory() as db:
            msg = PublicChatMessage(
                session_id=session_id,
                role="assistant",
                content=truncated,
            )
            db.add(msg)
            await self.renovar_actividad(session_id, db)
            await db.commit()

    async def close_session(self, session_id: str, session_token: str, db: AsyncSession) -> None:
        token_hash = _hash_key(session_token)
        result = await db.execute(
            select(PublicChatSession).where(
                PublicChatSession.id == session_id,
                PublicChatSession.session_token == token_hash,
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            return

        if session.status == "active":
            await self._archive_transcript(session, db)
            session.status = "closed"
            await db.commit()

    async def get_session_status(self, session_id: str, session_token: str, db: AsyncSession) -> dict:
        session = await self.validate_session(session_id, session_token, db)
        result = await db.execute(select(func.count()).where(PublicChatMessage.session_id == session_id))
        count = result.scalar() or 0
        return {
            "session_id": session.id,
            "status": session.status,
            "created_at": session.created_at,
            "last_activity": session.last_activity,
            "message_count": count,
        }

    # ── Session purge loop ──

    async def start_purge_loop(self) -> None:
        self._purge_task = asyncio.create_task(self._purge_loop())

    async def stop_purge_loop(self) -> None:
        if self._purge_task:
            self._purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._purge_task
            self._purge_task = None

    async def _purge_loop(self) -> None:
        while True:
            try:
                count = await self.purge_expired_sessions()
                if count:
                    logger.info("Purged %d expired public chat sessions", count)
            except Exception:
                logger.exception("Public chat purge failed")
            await asyncio.sleep(300)

    async def purge_expired_sessions(self) -> int:
        count = 0
        async with self.session_factory() as db:
            now = datetime.now(UTC)
            # Find expired sessions in small batches to avoid loading all into memory
            result = await db.execute(
                select(PublicChatSession)
                .where(
                    PublicChatSession.status == "active",
                    PublicChatSession.last_activity < now - timedelta(minutes=10),
                )
                .limit(100)
            )
            sessions = result.scalars().all()
            for session in sessions:
                elapsed = (now - session.last_activity).total_seconds()
                if elapsed > session.ttl_minutes * 60:
                    await self._archive_transcript(session, db)
                    session.status = "expired"
                    count += 1
            await db.commit()

            cutoff = now - timedelta(hours=1)
            await db.execute(
                delete(PublicChatSession).where(
                    PublicChatSession.status.in_(["closed", "expired"]),
                    PublicChatSession.last_activity < cutoff,
                )
            )
            await db.commit()
        return count

    async def _archive_transcript(self, session: PublicChatSession, db: AsyncSession) -> None:
        result = await db.execute(
            select(PublicChatMessage)
            .where(PublicChatMessage.session_id == session.id)
            .order_by(PublicChatMessage.created_at.asc())
        )
        messages = result.scalars().all()
        if not messages:
            return

        transcript = PublicChatTranscript(
            session_id=session.id,
            api_key_id=session.api_key_id,
            agent_id=session.agent_id,
            messages_json=[{"role": m.role, "content": m.content, "created_at": str(m.created_at)} for m in messages],
        )
        db.add(transcript)

    # ── API Key management ──

    async def create_api_key(
        self,
        label: str,
        agent_id: str,
        allowed_domains: list[str],
        requests_per_month: int,
        tokens_per_month: int,
        db: AsyncSession,
        widget_title: str | None = None,
        widget_theme: str = "auto",
        widget_accent: str = "#6366f1",
        widget_position: str = "right",
        bound_user_id: str | None = None,
        trust_sender_id: bool = False,
        bot_channel_id: str | None = None,
    ) -> dict:
        agent = await db.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")

        raw_key = f"pk_live_{secrets.token_urlsafe(32)}"
        key_hash = _hash_key(raw_key)
        key_prefix = raw_key[:12]

        api_key = PublicChatApiKey(
            key_hash=key_hash,
            key_prefix=key_prefix,
            label=label,
            agent_id=agent_id,
            allowed_domains=allowed_domains,
            requests_per_month=requests_per_month,
            tokens_per_month=tokens_per_month,
            widget_title=widget_title,
            widget_theme=widget_theme,
            widget_accent=widget_accent,
            widget_position=widget_position,
            bound_user_id=bound_user_id,
            trust_sender_id=trust_sender_id,
            bot_channel_id=bot_channel_id or None,
        )
        await self._check_channel(api_key, db)
        if bound_user_id or trust_sender_id:
            await self._check_binding(api_key, db)
        db.add(api_key)
        await db.commit()
        await db.refresh(api_key)

        return {
            "id": api_key.id,
            "raw_key": raw_key,
            "key_prefix": key_prefix,
            "label": label,
        }

    async def _check_channel(self, api_key: PublicChatApiKey, db: AsyncSession) -> None:
        if not api_key.bot_channel_id:
            return
        from hermeshq.models.bot_channel import BotChannel

        canal = await db.get(BotChannel, api_key.bot_channel_id)
        if canal is None or not canal.is_active:
            raise BindingError("Bot channel not found or inactive")

    async def _check_binding(self, api_key: PublicChatApiKey, db: AsyncSession) -> None:
        if _has_widget_config(api_key):
            raise BindingError("A key that carries a user identity cannot have widget configuration")
        if api_key.bound_user_id and api_key.trust_sender_id:
            raise BindingError("Choose either a bound user or trusted sender IDs, not both")
        if api_key.bound_user_id:
            problem = await _binding_problem(db, api_key.agent_id, api_key.bound_user_id)
            if problem:
                raise BindingError(problem)

    async def update_api_key(
        self,
        key_id: str,
        payload,
        db: AsyncSession,
    ) -> PublicChatApiKey:
        api_key = await db.get(PublicChatApiKey, key_id)
        if not api_key:
            raise ValueError("API key not found")
        update_data = payload.safe_update_dict()
        for field, value in update_data.items():
            setattr(api_key, field, value)
        if "bot_channel_id" in update_data:
            try:
                await self._check_channel(api_key, db)
            except BindingError:
                await db.rollback()
                raise
        if api_key.bound_user_id or api_key.trust_sender_id:
            try:
                await self._check_binding(api_key, db)
            except BindingError:
                await db.rollback()
                raise
        await db.commit()
        await db.refresh(api_key)
        return api_key

    async def permanently_delete_api_key(self, key_id: str, db: AsyncSession) -> None:
        api_key = await db.get(PublicChatApiKey, key_id)
        if not api_key:
            raise ValueError("API key not found")
        await db.execute(delete(PublicChatTranscript).where(PublicChatTranscript.api_key_id == key_id))
        await db.delete(api_key)
        await db.commit()

    async def list_api_keys(self, db: AsyncSession) -> list[PublicChatApiKey]:
        result = await db.execute(select(PublicChatApiKey).order_by(PublicChatApiKey.created_at.desc()))
        return list(result.scalars().all())

    async def delete_api_key(self, key_id: str, db: AsyncSession) -> None:
        api_key = await db.get(PublicChatApiKey, key_id)
        if not api_key:
            raise ValueError("API key not found")
        api_key.is_active = False
        await db.commit()

    async def list_transcripts(self, key_id: str, db: AsyncSession) -> list[PublicChatTranscript]:
        result = await db.execute(
            select(PublicChatTranscript)
            .where(PublicChatTranscript.api_key_id == key_id)
            .order_by(PublicChatTranscript.archived_at.desc())
            .limit(100)
        )
        return list(result.scalars().all())
