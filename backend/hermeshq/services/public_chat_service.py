import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from hermeshq.services.cancel_intent import (
    is_cancel_request,
    is_reset_request,
    is_status_request,
    split_reset_prefix,
)
from hermeshq.services.channel_user_resolver import resolve_channel_user
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)

PUBLIC_CHAT_USER_ID = "__public_chat__"
SENDER_PLATFORM = "microsoft_teams"
MAX_ACTIVE_SESSIONS_PER_KEY = 50
RACHA_FALLOS_MINIMA = 3
RACHA_FALLOS_VENTANA = 4
RACHA_HORAS = 4
RACHA_ERRORES = ("exceeded the task timeout", "produced no output for", "no result payload", "stopped early")
EXITO_REAL_SEGUNDOS = 90
EXITO_REAL_ITERACIONES = 4
MAX_ASSISTANT_MESSAGE_LENGTH = 32_768


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


class PublicChatService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        supervisor: object,
        event_broker: object,
    ) -> None:
        self.session_factory = session_factory
        self.supervisor = supervisor
        self.event_broker = event_broker
        self.rate_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)
        self.ip_rate_limiter = SimpleRateLimiter(max_requests=20, window_seconds=60)
        self.session_create_limiter = SimpleRateLimiter(max_requests=5, window_seconds=60)
        self.notification_limiter = SimpleRateLimiter(max_requests=120, window_seconds=60)
        self._purge_task: asyncio.Task | None = None

    async def validate_api_key(self, raw_key: str, db: AsyncSession, *, origin: str | None = None) -> PublicChatApiKey:
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
    ) -> dict:
        if client_ip and not self.session_create_limiter.check(client_ip):
            raise ValueError("Rate limit exceeded")

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
        chat_session = PublicChatSession(
            api_key_id=api_key.id,
            agent_id=agent_id,
            session_token=session_token_hash,
            ttl_minutes=10,
            sender_id=(sender_id or "").strip() or None if api_key.trust_sender_id else None,
        )
        db.add(chat_session)
        await db.commit()
        await db.refresh(chat_session)

        return {
            "session_id": chat_session.id,
            "session_token": raw_session_token,
            "agent_name": agent.friendly_name or agent.name,
        }

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
            user = await resolve_channel_user(db, SENDER_PLATFORM, session.sender_id)
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

    async def validate_session(self, session_id: str, session_token: str, db: AsyncSession) -> PublicChatSession:
        token_hash = _hash_key(session_token)
        result = await db.execute(
            select(PublicChatSession).where(
                PublicChatSession.id == session_id,
                PublicChatSession.session_token == token_hash,
                PublicChatSession.status == "active",
            )
        )
        session = result.scalar_one_or_none()
        if not session:
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

    @staticmethod
    def _task_belongs_to_chat(task: Task, session_id: str, thread_user_id: str) -> bool:
        metadata = task.metadata_json or {}
        if metadata.get("source") != "public_chat":
            return False
        if metadata.get("public_session_id") == session_id:
            return True
        return thread_user_id != PUBLIC_CHAT_USER_ID and metadata.get("thread_user_id") == thread_user_id

    @staticmethod
    def _reset_marker(session, session_id: str, thread_user_id: str, automatico: bool = False) -> Task:
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
                **({"auto_reset": True} if automatico else {}),
                "source": "public_chat",
                "public_session_id": session_id,
                "thread_user_id": thread_user_id,
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
        propias = [t for t in resultado.scalars().all() if self._task_belongs_to_chat(t, session_id, thread_user_id)]
        propias = [
            t for t in propias if (t.metadata_json or {}).get("conversation") or (t.metadata_json or {}).get("reset")
        ]
        return self.calcular_racha(propias)

    @staticmethod
    def _status_reply(tasks: list) -> str:
        ahora = utcnow()
        corriendo = [t for t in tasks if t.status == "running"]
        principal = (corriendo or tasks)[0]
        inicio = principal.started_at or principal.queued_at
        segundos = max(0, int((ahora - inicio).total_seconds())) if inicio else 0
        tiempo = f"{segundos // 60} min {segundos % 60:02d} s" if segundos >= 60 else f"{segundos} s"
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
        reset = is_reset_request(content)
        status = not reset and not is_cancel_request(content) and is_status_request(content)
        if not reset and not status and not is_cancel_request(content):
            return None
        if not self.rate_limiter.check(session_id):
            raise ValueError("Rate limit exceeded")
        if client_ip and not self.ip_rate_limiter.check(client_ip):
            raise ValueError("Rate limit exceeded")
        session = await self.validate_session(session_id, session_token, db)
        api_key = await db.get(PublicChatApiKey, session.api_key_id)
        thread_user_id, _ = await self._resolve_identity(api_key, session, db)
        session.last_activity = utcnow()
        result = await db.execute(
            select(Task).where(Task.agent_id == session.agent_id, Task.status.in_(("running", "queued")))
        )
        mine = [task for task in result.scalars().all() if self._task_belongs_to_chat(task, session_id, thread_user_id)]
        if status:
            if not mine:
                return None
            reply = self._status_reply(mine)
            db.add(PublicChatMessage(session_id=session_id, role="user", content=content))
            db.add(PublicChatMessage(session_id=session_id, role="assistant", content=reply))
            await db.commit()
            return reply
        active = getattr(self.supervisor, "active_tasks", {})
        for task in mine:
            if task.id in active:
                await self.supervisor.cancel_task(task.id)
            else:
                task.status = "cancelled"
                task.completed_at = utcnow()
                task.board_column = runtime_status_to_board_column("cancelled")
        if reset:
            db.add(self._reset_marker(session, session_id, thread_user_id))
            reply = "🧹 Listo, conversación reiniciada: empezamos de cero. ¿Qué necesitas?"
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
    ) -> tuple[str, str]:
        """Returns (task_id, agent_id) for SSE streaming."""
        if not self.rate_limiter.check(session_id):
            raise ValueError("Rate limit exceeded")
        if client_ip and not self.ip_rate_limiter.check(client_ip):
            raise ValueError("Rate limit exceeded")

        session = await self.validate_session(session_id, session_token, db)

        api_key = await db.get(PublicChatApiKey, session.api_key_id)
        if api_key:
            await self._check_monthly_quota(
                api_key.id,
                api_key.requests_per_month,
                api_key.tokens_per_month,
                db,
            )
        session.last_activity = utcnow()

        thread_user_id, binding_denied = await self._resolve_identity(api_key, session, db)

        resto = split_reset_prefix(content)
        if resto:
            db.add(self._reset_marker(session, session_id, thread_user_id))
            await db.flush()
            content = resto

        aviso_racha = None
        if not resto:
            racha = await self._racha_del_chat(db, session, session_id, thread_user_id)
            if self.debe_reiniciar(racha):
                db.add(self._reset_marker(session, session_id, thread_user_id, automatico=True))
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
        }
        if aviso_racha:
            task_metadata["auto_reset_aviso"] = aviso_racha
        if thread_user_id != PUBLIC_CHAT_USER_ID:
            task_metadata["public_chat_bound_user_id"] = thread_user_id
            if api_key and not api_key.bound_user_id:
                task_metadata["public_chat_sender_id"] = session.sender_id
        elif binding_denied:
            task_metadata["public_chat_binding_denied"] = binding_denied

        task = Task(
            agent_id=session.agent_id,
            created_by_user_id=None,
            title="Public chat message",
            prompt=content,
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

    async def save_assistant_message(self, session_id: str, content: str) -> None:
        truncated = content[:MAX_ASSISTANT_MESSAGE_LENGTH] if content else ""
        async with self.session_factory() as db:
            msg = PublicChatMessage(
                session_id=session_id,
                role="assistant",
                content=truncated,
            )
            db.add(msg)
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
        )
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
