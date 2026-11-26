from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

logger = logging.getLogger(__name__)

PROGRESS_NOTICE_SECONDS = 180
SCHEDULED_RETRY_WINDOW_MINUTES = 45


def clave_sesion_runtime(clave: str, epoca) -> str:
    try:
        numero = int(epoca or 0)
    except (TypeError, ValueError):
        numero = 0
    return f"{clave}#e{numero}" if numero > 0 else clave


HISTORY_USER_MARKER = "[Mensaje anterior, ya respondido; no lo repitas] "
HISTORY_FAILED_MARKER = (
    "[Solicitud anterior que NO se completó ni se entregó. Si el usuario pregunta por el informe, entregable o "
    "resultado, se refiere a esta solicitud y no a entregas anteriores] "
)
HISTORY_PENDING_MIN_CHARS = 60
HISTORY_MAX_TURNS = 6
HISTORY_MAX_TURNS_CADENA = 4
HISTORY_CANDIDATE_LIMIT = 200

CONVERSATION_KEY_FIELDS = ("thread_id", "public_session_id", "thread_user_id")


def conversation_key(metadata: dict | None) -> str:
    metadata = metadata or {}
    for field in CONVERSATION_KEY_FIELDS:
        value = str(metadata.get(field) or "").strip()
        if value:
            return value
    return ""


def history_assistant_text(task: Task, content: str, max_chars: int) -> str:
    if task.status in ("failed", "cancelled"):
        reason = (task.error_message or "").strip().splitlines()[0][:160] if task.error_message else ""
        suffix = f": {reason}" if reason else ""
        return f"(Turno anterior sin completar{suffix}. No se entregó una respuesta final.)"
    content = sin_avisos_de_la_plataforma(content) or content
    if max_chars > 0 and len(content) > max_chars:
        cabeza = int(max_chars * 0.6)
        cola = max_chars - cabeza
        omitidos = len(content) - max_chars
        return (
            content[:cabeza].rstrip()
            + f"\n…(respuesta anterior recortada: {omitidos} caracteres omitidos)…\n"
            + content[-cola:].lstrip()
        )
    return content


def apply_usage_metadata(task: Task, usage: dict | None) -> None:
    if not usage:
        return
    metadata = dict(task.metadata_json or {})
    metadata["usage"] = usage
    task.metadata_json = metadata


def apply_failure_telemetry(task: Task, exc: BaseException, default_reason: str = "runtime_error") -> None:
    partial = getattr(exc, "hq_partial", None)
    metadata = dict(task.metadata_json or {})
    if isinstance(partial, dict):
        tool_calls = partial.get("tool_calls")
        if tool_calls:
            task.tool_calls = tool_calls
        iterations = partial.get("iterations")
        if iterations:
            task.iterations = max(int(task.iterations or 0), int(iterations))
        metadata["end_reason"] = partial.get("end_reason") or default_reason
        if partial.get("error_type"):
            metadata["error_type"] = partial["error_type"]
        if partial.get("error_trace"):
            metadata["error_trace"] = partial["error_trace"]
        if partial.get("run"):
            metadata["run"] = partial["run"]
        run_usage = (partial.get("run") or {}).get("usage")
        if run_usage:
            metadata["usage"] = run_usage
    else:
        metadata["end_reason"] = default_reason
    metadata.setdefault("error_type", type(exc).__name__)
    task.metadata_json = metadata


class _ResizableLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    async def resize(self, limit: int) -> None:
        async with self._condition:
            self._limit = limit
            self._condition.notify_all()


class _StreamBuffer:
    """Accumulates streaming deltas and flushes them to the DB in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_id: str,
        agent_id: str,
        log_func,
        event_broker: EventBroker,
        flush_interval: float = 0.5,
        user_id: str | None = None,
        claim_token: str | None = None,
        audience: EventAudience | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_id = task_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._claim_token = claim_token
        self._audience = audience
        self._log_func = log_func
        self._event_broker = event_broker
        self._flush_interval = flush_interval
        self._deltas: list[tuple[str, int | None]] = []
        self._flush_task: asyncio.Task | None = None

    # -- public API ----------------------------------------------------------

    def start_flush_loop(self) -> None:
        """Start the periodic flush background task."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop_flush_loop(self) -> None:
        """Cancel the periodic flush and drain remaining deltas."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        # Final drain of any remaining buffered deltas.
        await self.flush()

    async def push(self, delta: str, index: int | None = None) -> None:
        delta = delta if isinstance(delta, str) else ""
        """Append a delta to the buffer and publish an event immediately."""
        self._deltas.append((delta, index))
        event = {
            "type": "task.progress",
            "task_id": self._task_id,
            "agent_id": self._agent_id,
            "message": delta,
            "step": index,
        }
        if self._user_id:
            event["created_by_user_id"] = self._user_id
        await self._event_broker.publish(
            event,
            audience=self._audience or EventAudience.for_agent(self._agent_id, user_id=self._user_id),
        )

    async def flush(self) -> None:
        """Write all buffered deltas to the DB in a single transaction."""
        if not self._deltas:
            return

        snapshots = self._deltas[:]
        self._deltas.clear()

        # Collapse deltas into a single content string per batch so that
        # messages_json grows by *one* entry per flush instead of N.
        combined_content = "".join(d for d, _ in snapshots)
        max_index: int | None = None
        for _, idx in snapshots:
            if idx is not None:
                max_index = idx if max_index is None else max(max_index, idx)

        try:
            async with self._session_factory() as session:
                statement = select(Task).where(Task.id == self._task_id)
                if self._claim_token:
                    statement = statement.where(
                        Task.status == "running",
                        Task.claim_token == self._claim_token,
                    )
                task_row = (await session.execute(statement)).scalar_one_or_none()
                agent_row = await session.get(Agent, task_row.agent_id) if task_row else None
                if not task_row or not agent_row:
                    return

                task_row.messages_json = [
                    *task_row.messages_json,
                    {"role": "assistant", "content": combined_content},
                ]
                if max_index is not None:
                    task_row.iterations = max(task_row.iterations, max_index)
                await self._log_func(
                    session,
                    "agent.output",
                    agent=agent_row,
                    task=task_row,
                    message=combined_content[:240],
                    details=(
                        {"step": max_index}
                        if max_index is not None
                        else {
                            "engine": "hermes-agent",
                            "batch_size": len(snapshots),
                        }
                    ),
                )
                agent_row.last_activity = utcnow()
                await session.commit()
        except Exception:
            self._deltas = snapshots + self._deltas
            raise

    # -- internals -----------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodic background flush."""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self.flush()
                except Exception:  # noqa: BLE001  # periodic flush — any error logged
                    logger.exception("StreamBuffer periodic flush error")
        except asyncio.CancelledError:
            return


from hermeshq.config import get_settings
from hermeshq.core.events import EventAudience, EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.message import AgentMessage
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.secret import Secret
from hermeshq.models.task import DEFAULT_EXECUTION_LANE, Task
from hermeshq.services import admision, desglose_tiempos, referencias_correo
from hermeshq.services.auto_background import (
    create_background_continuation,
    create_continuation,
    error_de_incompleto,
    motivo_resultado_incompleto,
)
from hermeshq.services.ejecucion_aprobada import MARCA_EJECUTADA
from hermeshq.services.execution_lease import (
    INSTANCE_OWNER,
    ExecutionLeaseError,
    ExecutionReconciler,
    IdentityUnconfirmedError,
    ReconciliationRequiredError,
    clasificar_interrupcion,
    entrega_tras_reencolar,
    lease_settings,
    mensaje_de_fallo,
)
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.respuesta_sin_repeticion import filtrar_respuesta
from hermeshq.services.respuesta_verificada import (
    con_aviso_accion_graph_no_confirmada,
    con_aviso_de_adjunto,
    con_aviso_descarte_o_marcado_no_confirmado,
    con_aviso_envio_no_confirmado,
    con_aviso_sin_herramienta,
    con_borrador_canonico,
    con_informe_sin_lectura,
    con_tablas_de_correo,
    sin_avisos_de_la_plataforma,
)
from hermeshq.services.schedule_notifications import (
    enqueue_delegation_notice,
    enqueue_handoff_notice,
    enqueue_progress_notice,
    enqueue_respuesta_tardia,
    enqueue_schedule_notification,
    es_tarea_de_delegacion_del_servidor,
)
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.task_board import sync_board_with_runtime
from hermeshq.services.task_lanes import task_class, task_class_expression
from hermeshq.services.task_visibility import task_event_audience


class HistorialConversacion(list):
    herramientas: list[str] = []
    omitidos: int = 0
    por_antiguedad: int = 0


def _termina_en_pregunta(turno) -> bool:
    texto = str(getattr(turno, "response", "") or "").rstrip().rstrip("*_)\"'» ").rstrip()
    return texto.endswith("?")


def aviso_de_corte_del_historial(historial) -> str | None:
    omitidos = int(getattr(historial, "omitidos", 0) or 0)
    antiguos = int(getattr(historial, "por_antiguedad", 0) or 0)
    if omitidos <= 0 and antiguos <= 0:
        return None
    partes = []
    if omitidos > 0:
        partes.append(f"{omitidos} turno(s) anterior(es) quedaron fuera de lo que ves (solo conservas los últimos)")
    if antiguos > 0:
        partes.append(f"{antiguos} turno(s) son de hace demasiado tiempo y no los ves")
    return (
        "Esta conversación se cortó: "
        + " y ".join(partes)
        + ". Si el usuario se refiere a algo anterior que no aparece en lo que ves, díselo con claridad (que esa parte "
        "de la conversación ya no la tienes a la vista) y pídele que lo repita; no lo inventes ni lo des por sabido."
    )


class AgentSupervisor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        runtime: HermesRuntime,
        secret_vault: SecretVault,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.secret_vault = secret_vault
        self.runtimes: dict[str, Any] = {"hermes": runtime}
        self.running_agents: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.gateway_supervisor: object | None = None
        settings = get_settings()
        self._concurrency_limiter = _ResizableLimiter(settings.concurrency_semaphore)
        self._semaphore_value = settings.concurrency_semaphore
        self._queue_poll_seconds = settings.task_queue_poll_seconds
        self._lease_seconds = settings.task_lease_seconds
        self._heartbeat_seconds = settings.task_heartbeat_seconds
        self._max_attempts = settings.task_max_attempts
        self._worker_id = str(uuid4())
        self._dispatcher_task: asyncio.Task | None = None
        self._dispatch_wakeup = asyncio.Event()
        self._active_claims: dict[str, str] = {}
        self._active_classes: dict[str, str] = {}
        self._requeue_on_cancel: set[str] = set()
        self._stopping = False
        self._reconcile_task: asyncio.Task | None = None
        self._configurar_admision(settings.concurrency_semaphore)

    @property
    def runtime(self) -> Any:
        return self.runtimes["hermes"]

    def register_runtime(self, runtime_type: str, runtime: object) -> None:
        """Register an additional runtime (e.g. 'pi' for PiRuntime)."""
        self.runtimes[runtime_type] = runtime

    def _limites_admision(self, total_slots: int) -> tuple[admision.Limites, list[str]]:
        settings = get_settings()
        return admision.normalizar(
            total_slots,
            settings.concurrency_interactive_reserve,
            settings.concurrency_background_max,
            settings.concurrency_aging_seconds,
        )

    def _configurar_admision(self, total_slots: int) -> None:
        limites, avisos = self._limites_admision(total_slots)
        for aviso in avisos:
            logger.warning("Concurrencia: %s", aviso)
        control = getattr(self, "_admision", None)
        if control is None:
            self._admision = admision.ControlAdmision(limites)
        else:
            control.configurar(limites)

    async def update_semaphore(self, new_value: int) -> None:
        self._semaphore_value = new_value
        self._configurar_admision(new_value)
        await self._concurrency_limiter.resize(new_value)
        self._dispatch_wakeup.set()

    def _build_conversation_assistant_content(self, task: Task) -> str:
        if task.response and task.response.strip():
            return task.response.strip()
        streamed = "".join(
            str(message.get("content") or "")
            for message in (task.messages_json or [])
            if message.get("role") == "assistant"
        ).strip()
        if streamed:
            return streamed
        if task.status == "failed" and task.error_message:
            return task.error_message.strip()
        return ""

    async def _build_conversation_history(self, session: AsyncSession, task: Task) -> list[dict]:
        metadata = task.metadata_json or {}
        if not metadata.get("conversation"):
            return []
        convo_key = conversation_key(metadata)
        owner = getattr(task, "created_by_user_id", None)

        result = await session.execute(
            select(Task)
            .where(Task.agent_id == task.agent_id)
            .order_by(desc(Task.queued_at))
            .limit(HISTORY_CANDIDATE_LIMIT)
        )
        candidates = list(result.scalars().all())
        settings = get_settings()
        max_age = max(0, int(settings.conversation_history_max_age_hours))
        max_chars = max(0, int(settings.conversation_history_turn_max_chars))
        cutoff = utcnow() - timedelta(hours=max_age) if max_age else None
        de_la_conversacion = [
            item
            for item in reversed(candidates)
            if item.id != task.id
            and (item.metadata_json or {}).get("conversation")
            and conversation_key(item.metadata_json) == convo_key
            and (convo_key or getattr(item, "created_by_user_id", None) == owner)
        ]
        prior_turns = [
            item for item in de_la_conversacion if cutoff is None or item.queued_at is None or item.queued_at >= cutoff
        ]
        por_antiguedad = sum(
            1 for item in de_la_conversacion if item.status not in ("failed", "cancelled") and item not in prior_turns
        )
        history: list[dict] = []
        for indice in range(len(prior_turns) - 1, -1, -1):
            if (prior_turns[indice].metadata_json or {}).get("reset"):
                prior_turns = prior_turns[indice + 1 :]
                break
        ultimo = len(prior_turns) - 1
        pendiente = None
        for indice in range(ultimo, -1, -1):
            item = prior_turns[indice]
            if item.status not in ("failed", "cancelled"):
                break
            if len((item.prompt or "").strip()) >= HISTORY_PENDING_MIN_CHARS:
                pendiente = indice
                break
        forzados = set()
        if pendiente is not None:
            forzados.add(pendiente)
        if ultimo >= 0 and prior_turns[ultimo].status == "failed":
            forzados.add(ultimo)
        completados = [
            indice
            for indice, item in enumerate(prior_turns)
            if item.status not in ("failed", "cancelled") and indice not in forzados
        ]
        conservar = set(completados[-HISTORY_MAX_TURNS:]) | forzados
        posicion_mas_antigua = max(0, len(completados) - HISTORY_MAX_TURNS)
        agregados = 0
        while (
            posicion_mas_antigua > 0
            and agregados < HISTORY_MAX_TURNS_CADENA
            and _termina_en_pregunta(prior_turns[completados[posicion_mas_antigua - 1]])
        ):
            posicion_mas_antigua -= 1
            agregados += 1
            conservar.add(completados[posicion_mas_antigua])
        relevantes = [item for indice, item in enumerate(prior_turns) if indice in conservar]
        resumen = await self._resumen_programado_entregado(session, task, candidates, convo_key, relevantes)
        entradas: list[tuple] = [(prior, None) for prior in relevantes]
        if resumen is not None:
            entregado, cron_task = resumen
            posicion = sum(1 for prior in relevantes if prior.queued_at is None or prior.queued_at <= entregado)
            entradas.insert(posicion, (cron_task, entregado))
        for prior, entregado in entradas:
            if entregado is not None:
                from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, resolve_timezone

                contenido = self._build_conversation_assistant_content(prior)
                nombre = " ".join(str(prior.title or "tarea programada").split())[:120]
                fecha = entregado.astimezone(resolve_timezone(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")
                history.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Resumen programado «{nombre}» task_id={prior.id}, {fecha}: resultado de una tarea "
                            "programada que el sistema le entregó al usuario por este chat. Si el usuario se refiere "
                            "a algo de ese resumen («el segundo», «respóndele», el formato), habla de este resumen; "
                            "no lo repitas entero"
                            + (
                                ". Para saber a qué correo exacto se refiere llama ms365_correo_sync con "
                                "accion='referencia' y su frase; no lo deduzcas de este texto]"
                                if (prior.metadata_json or {}).get("resumen_correo")
                                else "]"
                            )
                        ),
                    }
                )
                texto = history_assistant_text(prior, contenido or "(sin contenido)", max_chars)
                history.append({"role": "assistant", "content": texto})
                continue
            if prior.prompt.strip():
                marker = HISTORY_FAILED_MARKER if prior.status in ("failed", "cancelled") else HISTORY_USER_MARKER
                history.append({"role": "user", "content": f"{marker}{prior.prompt.strip()}"})
            assistant_content = self._build_conversation_assistant_content(prior)
            if prior.status in ("failed", "cancelled") or assistant_content:
                history.append(
                    {"role": "assistant", "content": history_assistant_text(prior, assistant_content, max_chars)}
                )
        historial = HistorialConversacion(history)
        historial.omitidos = max(0, len(completados) - len([i for i in conservar if i in set(completados)]))
        historial.por_antiguedad = por_antiguedad
        historial.herramientas = sorted(
            {
                str(c.get("name"))
                for p, _ in entradas
                for c in (getattr(p, "tool_calls", None) or [])
                if isinstance(c, dict) and c.get("name")
            }
        )
        return historial

    async def _resumen_programado_entregado(
        self, session: AsyncSession, task: Task, candidatos: list, convo_key: str, relevantes: list
    ) -> tuple | None:
        from hermeshq.models.public_chat import PublicChatNotification

        metadata = task.metadata_json or {}
        user_id = str(metadata.get("thread_user_id") or "").strip()
        if not user_id or user_id.startswith("__") or metadata.get("scheduled") or metadata.get("background_job"):
            return None
        ahora = utcnow()
        limite_superior = getattr(task, "queued_at", None) or ahora
        desde = ahora - referencias_correo.VENTANA_IMPLICITA
        clave_propia = str(metadata.get("public_chat_api_key_id") or "")
        for item in candidatos:
            md = item.metadata_json or {}
            if (md.get("reset") or md.get("auto_reset")) and item.queued_at is not None:
                clave_del_reset = str(md.get("public_chat_api_key_id") or "")
                otra_clave = bool(clave_propia and clave_del_reset and clave_del_reset != clave_propia)
                if conversation_key(md) == convo_key or (md.get("thread_user_id") == user_id and not otra_clave):
                    desde = max(desde, item.queued_at)
        if len(relevantes) >= HISTORY_MAX_TURNS and relevantes[0].queued_at is not None:
            desde = max(desde, relevantes[0].queued_at)
        filtros = [
            PublicChatNotification.agent_id == task.agent_id,
            PublicChatNotification.recipient_user_id == user_id,
            PublicChatNotification.schedule_id.isnot(None),
            PublicChatNotification.status == "delivered",
            PublicChatNotification.delivered_at > desde,
            PublicChatNotification.delivered_at <= limite_superior,
            Task.agent_id == task.agent_id,
        ]
        if clave_propia:
            filtros.append(PublicChatNotification.api_key_id == clave_propia)
        fila = (
            await session.execute(
                select(PublicChatNotification.delivered_at, PublicChatNotification.schedule_id, Task)
                .join(Task, Task.id == PublicChatNotification.task_id)
                .where(*filtros)
                .order_by(desc(PublicChatNotification.delivered_at))
                .limit(1)
            )
        ).first()
        if fila is None:
            return None
        entregado, schedule_id, cron_task = fila
        md = cron_task.metadata_json or {}
        identidades = {
            str(valor) for valor in (cron_task.created_by_user_id, md.get("thread_user_id")) if valor not in (None, "")
        }
        if (
            cron_task.id == task.id
            or not md.get("scheduled")
            or str(md.get("scheduled_task_id") or "") != str(schedule_id)
            or identidades != {user_id}
        ):
            return None
        return entregado, cron_task

    async def bootstrap_runtime(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent).where(Agent.status == "running"))
            for agent in result.scalars().all():
                self.running_agents.add(agent.id)

        await self._recover_expired_tasks()
        self._stopping = False
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        self.start_reconcile_loop()
        self._dispatch_wakeup.set()

    async def shutdown_runtime(self) -> None:
        self._stopping = True
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher_task
            self._dispatcher_task = None
        runners = list(self.active_tasks.items())
        self._requeue_on_cancel.update(task_id for task_id, _ in runners)
        for _, runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*(runner for _, runner in runners), return_exceptions=True)
        try:
            await self._requeue_worker_claims()
        except Exception:
            logger.exception("Failed to release task claims during shutdown")

    def _reconciler(self) -> ExecutionReconciler:
        reconciler = getattr(self, "_execution_reconciler", None)
        if reconciler is None:
            leases = getattr(self.runtime, "leases", None)
            reconciler = ExecutionReconciler(
                self.session_factory,
                getattr(self.runtime, "local_runners", None),
                owner=leases.owner if leases is not None else INSTANCE_OWNER,
            )
            self._execution_reconciler = reconciler
        return reconciler

    async def reconcile_executions(self) -> dict | None:
        try:
            return await self._reconciler().reconcile()
        except Exception:  # noqa: BLE001
            logger.exception("Execution attempt reconciliation failed")
            return None

    def start_reconcile_loop(self) -> None:
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(lease_settings()["reconcile_interval"])
                await self.reconcile_executions()

        self._reconcile_task = asyncio.create_task(_loop())

    @staticmethod
    def _release_claim(task: Task) -> None:
        task.claimed_by = None
        task.claim_token = None
        task.claimed_at = None
        task.lease_expires_at = None

    async def _interrupted_outcome(self, session: AsyncSession, task: Task, now) -> tuple[str, list[dict]]:
        clase, acciones = await clasificar_interrupcion(session, task)
        if clase == "interrumpida" and not self._scheduled_retry_in_window(task, now):
            clase = "programada_vencida"
        return clase, acciones

    @staticmethod
    def _scheduled_retry_in_window(task: Task, now: datetime) -> bool:
        metadata = task.metadata_json or {}
        if not metadata.get("scheduled") or metadata.get("manual_trigger") or task.queued_at is None:
            return True
        queued_at = task.queued_at if task.queued_at.tzinfo else task.queued_at.replace(tzinfo=now.tzinfo)
        return bool((now - queued_at).total_seconds() <= SCHEDULED_RETRY_WINDOW_MINUTES * 60)

    async def _settle_interrupted(
        self, session: AsyncSession, task: Task, now, motivo: str, *, attempts_used: int
    ) -> tuple[str, str]:
        clase, acciones = await self._interrupted_outcome(session, task, now)
        self._release_claim(task)
        metadata = dict(task.metadata_json or {})
        if clase == "interrumpida" and attempts_used < self._max_attempts:
            task.status = "queued"
            task.started_at = None
            task.completed_at = None
            task.error_message = None
            task.cancel_requested_at = None
            task.metadata_json = entrega_tras_reencolar(metadata)
            await sync_board_with_runtime(session, task.id, task.status)
            return "task.requeued", "queued"
        task.status = "failed"
        task.completed_at = now
        task.cancel_requested_at = None
        if clase == "interrumpida":
            task.error_message = f"Task lease expired after {task.attempt_count} execution attempts."
        else:
            task.error_message = mensaje_de_fallo(clase)
        task.metadata_json = {
            **metadata,
            "recovery": {
                "estado": clase,
                "motivo": motivo,
                "reintentable": clase in ("interrumpida", "programada_vencida"),
                **(
                    {"acciones": acciones, "requiere_conciliacion": any(a["estado"] == "iniciada" for a in acciones)}
                    if acciones
                    else {}
                ),
            },
        }
        await sync_board_with_runtime(session, task.id, task.status)
        await self._on_execution_settled(session, task, clase)
        return "task.failed", "failed"

    async def _requeue_worker_claims(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(Task.status == "running", Task.claimed_by == self._worker_id)
                .with_for_update(skip_locked=True)
            )
            tasks = list(result.scalars().all())
            now = utcnow()
            for task in tasks:
                if task.cancel_requested_at is not None:
                    task.status = "cancelled"
                    task.completed_at = now
                    self._release_claim(task)
                    await sync_board_with_runtime(session, task.id, task.status)
                    event_type, message = "task.cancelled", "Task cancelled during shutdown"
                else:
                    task.attempt_count = max(0, task.attempt_count - 1)
                    event_type, estado = await self._settle_interrupted(
                        session, task, now, "apagado_del_servicio", attempts_used=task.attempt_count
                    )
                    message = (
                        "Task returned to queue during shutdown"
                        if estado == "queued"
                        else "Task interrupted during shutdown after possible external actions"
                    )
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type=event_type,
                        message=message,
                        details={"attempt_count": task.attempt_count},
                    )
                )
            if tasks:
                await session.commit()

    async def _recover_expired_tasks(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(
                    Task.status == "running",
                    or_(Task.lease_expires_at.is_(None), Task.lease_expires_at <= utcnow()),
                )
                .with_for_update(skip_locked=True)
            )
            expired = result.scalars().all()
            if not expired:
                return 0

            now = utcnow()
            for task in expired:
                if task.cancel_requested_at is not None:
                    task.status = "cancelled"
                    task.completed_at = now
                    self._release_claim(task)
                    await sync_board_with_runtime(session, task.id, task.status)
                    event_type = "task.cancelled"
                    message = "Expired execution cancelled"
                else:
                    event_type, estado = await self._settle_interrupted(
                        session, task, now, "lease_vencido", attempts_used=task.attempt_count
                    )
                    message = (
                        "Expired execution returned to queue"
                        if estado == "queued"
                        else "Task exceeded recovery attempts or may have run external actions"
                    )
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type=event_type,
                        message=message,
                        details={"attempt_count": task.attempt_count},
                    )
                )

            await session.commit()
            logger.warning("Recovered %d tasks with expired execution leases", len(expired))
            return len(expired)

    async def _on_execution_settled(self, session: AsyncSession, task: Task, clase: str) -> None:
        metadata = task.metadata_json or {}
        if metadata.get("background_job"):
            await enqueue_schedule_notification(
                session,
                task,
                success=False,
                summary="el servidor se reinició mientras lo trabajaba; vuelve a pedirlo",
            )
            return
        await enqueue_respuesta_tardia(session, task, success=False, summary=mensaje_de_fallo(clase))
        if metadata.get("scheduled"):
            await enqueue_schedule_notification(session, task, success=False, summary=mensaje_de_fallo(clase))

    @staticmethod
    async def _schedule_still_authorized(session: AsyncSession, task: Task) -> bool:
        metadata = task.metadata_json or {}
        schedule_id = metadata.get("scheduled_task_id")
        if not metadata.get("scheduled") or metadata.get("manual_trigger") or not schedule_id:
            return True
        from hermeshq.models.scheduled_task import ScheduledTask

        schedule = await session.get(ScheduledTask, schedule_id)
        return schedule is not None and bool(schedule.enabled) and schedule.approval_status == "approved"

    async def start_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            if agent.is_archived:
                raise ValueError("Archived agents cannot be started")
            agent.status = "running"
            agent.last_activity = utcnow()
            self.running_agents.add(agent.id)
            await self._log(session, "agent.started", agent=agent, message=f"{agent.name} started")
            await session.commit()
            await session.refresh(agent)
        await self.event_broker.publish(
            {
                "type": "agent.status_changed",
                "agent_id": agent_id,
                "status": "running",
            },
            audience=EventAudience.for_agent(agent_id),
        )
        await self._start_pending_tasks(agent_id)
        await self._ensure_gateways_alive(agent_id)
        return agent

    async def _ensure_gateways_alive(self, agent_id: str) -> None:
        gw = getattr(self, "gateway_supervisor", None)
        if gw is None:
            return
        from hermeshq.models.messaging_channel import MessagingChannel

        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == agent_id,
                    MessagingChannel.enabled.is_(True),
                )
            )
            channels = result.scalars().all()

        for channel in channels:
            if channel.platform in ("google_chat", "kapso_whatsapp"):
                continue
            handle = gw.processes.get(agent_id)
            is_alive = handle and handle.process.poll() is None and channel.platform in handle.platforms
            if not is_alive:
                logger.info("Reviving dead gateway for %s/%s on agent start", agent_id, channel.platform)
                try:
                    await gw.start_channel(agent_id, channel.platform)
                except Exception:
                    logger.warning("Failed to revive gateway %s/%s", agent_id, channel.platform, exc_info=True)

    async def stop_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            agent.status = "stopped"
            agent.last_activity = utcnow()
            self.running_agents.discard(agent.id)
            await self._log(session, "agent.stopped", agent=agent, message=f"{agent.name} stopped")
            await session.commit()
            await session.refresh(agent)
        await self.event_broker.publish(
            {
                "type": "agent.status_changed",
                "agent_id": agent_id,
                "status": "stopped",
            },
            audience=EventAudience.for_agent(agent_id),
        )
        return agent

    async def restart_agent(self, agent_id: str) -> Agent:
        await self.stop_agent(agent_id)
        return await self.start_agent(agent_id)

    def _workspace_de(self, agent):
        if agent is None:
            return None
        try:
            return self.runtime.installation_manager.resolve_workspace_path(agent.workspace_path)
        except Exception:  # noqa: BLE001
            return None

    async def submit_task(self, task_id: str) -> None:
        self._dispatch_wakeup.set()

    async def _run_claimed_task(self, task_id: str, claim_token: str) -> None:
        execution_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        try:
            execution_task = asyncio.create_task(self._run_task(task_id, claim_token))
            heartbeat_task = asyncio.create_task(self._heartbeat_claim(task_id, claim_token, execution_task))
            await execution_task
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if execution_task is not None and not execution_task.done():
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
            clase = self._active_classes.pop(task_id, None)
            if clase is not None:
                self._admision.liberar(clase)
            await self._concurrency_limiter.release()
            self.active_tasks.pop(task_id, None)
            self._active_claims.pop(task_id, None)
            self._requeue_on_cancel.discard(task_id)
            self._dispatch_wakeup.set()

    async def cancel_task(self, task_id: str) -> None:
        publish_event: dict | None = None
        async with self.session_factory() as session:
            task = (
                await session.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one_or_none()
            if task is None or task.status not in {"pending", "queued", "running"}:
                return
            if task.status in {"pending", "queued"}:
                task.status = "cancelled"
                task.completed_at = utcnow()
                task.cancel_requested_at = utcnow()
                await sync_board_with_runtime(session, task.id, task.status)
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type="task.cancelled",
                        message=task.title or "Task cancelled",
                    )
                )
                publish_event = {
                    "type": "task.cancelled",
                    "task_id": task.id,
                    "agent_id": task.agent_id,
                }
                if task.created_by_user_id:
                    publish_event["created_by_user_id"] = task.created_by_user_id
            else:
                task.cancel_requested_at = utcnow()
            await session.commit()

        runner = self.active_tasks.get(task_id)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        else:
            self._dispatch_wakeup.set()
        if publish_event is not None:
            await self.event_broker.publish(
                publish_event,
                audience=EventAudience.for_agent(
                    publish_event["agent_id"],
                    user_id=publish_event.get("created_by_user_id"),
                ),
            )

    async def _start_pending_tasks(self, agent_id: str) -> None:
        self._dispatch_wakeup.set()

    async def _dispatch_loop(self) -> None:
        try:
            while not self._stopping:
                self._dispatch_wakeup.clear()
                try:
                    await self._recover_expired_tasks()
                    await self._expire_stale_queued_tasks()
                    await self._cancel_requested_claims()
                    await self._dispatch_available_tasks()
                except Exception:
                    logger.exception("Durable task dispatcher tick failed")
                try:
                    await asyncio.wait_for(self._dispatch_wakeup.wait(), timeout=self._queue_poll_seconds)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _dispatch_available_tasks(self) -> None:
        while not self._stopping and len(self.active_tasks) < self._semaphore_value:
            await self._concurrency_limiter.acquire()
            try:
                claim = await self._claim_next_task()
            except BaseException:
                await self._concurrency_limiter.release()
                raise
            if claim is None:
                await self._concurrency_limiter.release()
                return
            task_id, claim_token = claim
            runner = asyncio.create_task(self._run_claimed_task(task_id, claim_token))
            self.active_tasks[task_id] = runner
            self._active_claims[task_id] = claim_token

    async def _expire_stale_queued_tasks(self) -> None:
        timeout = int(get_settings().public_chat_queue_timeout_seconds)
        if timeout <= 0:
            return
        cutoff = utcnow() - timedelta(seconds=timeout)
        events: list[tuple[dict, EventAudience | None]] = []
        async with self.session_factory() as session:
            result = await session.execute(select(Task).where(Task.status == "queued", Task.queued_at < cutoff))
            stale = [
                task for task in result.scalars().all() if (task.metadata_json or {}).get("source") == "public_chat"
            ]
            if not stale:
                return
            message = f"Task waited {timeout}s in the queue without a free slot"
            for task in stale:
                task.status = "failed"
                task.completed_at = utcnow()
                task.error_message = message
                task.metadata_json = {
                    **(task.metadata_json or {}),
                    "end_reason": "queue_timeout",
                    "error_type": "QueueTimeout",
                }
                await sync_board_with_runtime(session, task.id, task.status)
                agent = await session.get(Agent, task.agent_id)
                if agent:
                    await self._log(
                        session,
                        "task.failed",
                        agent=agent,
                        task=task,
                        message=task.title or "Task failed",
                        details={"error": message, "error_type": "QueueTimeout"},
                    )
                event: dict = {
                    "type": "task.failed",
                    "task_id": task.id,
                    "agent_id": task.agent_id,
                    "error": message,
                    "error_type": "QueueTimeout",
                }
                if task.created_by_user_id:
                    event["created_by_user_id"] = task.created_by_user_id
                events.append((event, task_event_audience(task)))
            await session.commit()
        for event, audience in events:
            await self.event_broker.publish(event, audience=audience)

    def _admitted_classes(self) -> list[str]:
        return [clase for clase in admision.CLASES if self._admision.cabe(clase)]

    async def _claim_next_task(self) -> tuple[str, str] | None:
        admitted = self._admitted_classes()
        if not admitted:
            return None
        parallel_lanes = bool(get_settings().task_parallel_lanes)
        queued_task = aliased(Task)
        running_task = aliased(Task)
        queued_class = task_class_expression(queued_task)

        def lane_is_busy(candidate):
            condition = [running_task.agent_id == candidate.agent_id, running_task.status == "running"]
            if parallel_lanes:
                condition.append(running_task.execution_lane == task_class_expression(candidate))
            return exists(select(running_task.id).where(*condition))

        head_queued_at = (
            select(queued_task.queued_at)
            .where(
                queued_task.agent_id == Agent.id,
                queued_task.status == "queued",
                queued_class.in_(admitted),
                ~lane_is_busy(queued_task),
            )
            .order_by(queued_task.queued_at.asc(), queued_task.id.asc())
            .limit(1)
            .scalar_subquery()
        )
        async with self.session_factory() as session:
            agent = (
                await session.execute(
                    select(Agent)
                    .where(
                        Agent.status == "running",
                        Agent.is_archived.is_(False),
                        head_queued_at.is_not(None),
                    )
                    .order_by(head_queued_at.asc(), Agent.id.asc())
                    .with_for_update(of=Agent, skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if agent is None:
                return None

            candidate = aliased(Task)
            task = (
                await session.execute(
                    select(candidate)
                    .where(
                        candidate.agent_id == agent.id,
                        candidate.status == "queued",
                        task_class_expression(candidate).in_(admitted),
                        ~lane_is_busy(candidate),
                    )
                    .order_by(candidate.queued_at.asc(), candidate.id.asc())
                    .with_for_update(of=candidate)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            clase = task_class(task)
            if not self._admision.cabe(clase):
                return None

            now = utcnow()
            claim_token = str(uuid4())
            task.status = "running"
            task.execution_lane = clase if parallel_lanes else DEFAULT_EXECUTION_LANE
            task.claimed_by = self._worker_id
            task.claim_token = claim_token
            task.claimed_at = now
            task.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            task.cancel_requested_at = None
            task.attempt_count += 1
            task.started_at = now
            task.completed_at = None
            task.error_message = None
            task.messages_json = []
            task.tool_calls = []
            task.metadata_json = {
                **(task.metadata_json or {}),
                "admision": {
                    "clase": clase,
                    "en_cola_at": task.queued_at.isoformat() if task.queued_at else None,
                    "admitida_at": now.isoformat(),
                },
            }
            await sync_board_with_runtime(session, task.id, task.status)
            await self._log(
                session,
                "task.started",
                agent=agent,
                task=task,
                message=task.title or task.prompt[:72],
                details={"attempt_count": task.attempt_count, "worker_id": self._worker_id},
            )
            await session.commit()
            self._admision.ocupar(clase)
            self._active_classes[task.id] = clase
            return task.id, claim_token

    async def _heartbeat_claim(
        self,
        task_id: str,
        claim_token: str,
        execution_task: asyncio.Task,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                try:
                    async with self.session_factory() as session:
                        result = await session.execute(
                            update(Task)
                            .where(
                                Task.id == task_id,
                                Task.status == "running",
                                Task.claim_token == claim_token,
                                Task.cancel_requested_at.is_(None),
                            )
                            .values(lease_expires_at=utcnow() + timedelta(seconds=self._lease_seconds))
                        )
                        await session.commit()
                        if getattr(result, "rowcount", 0) == 1:
                            continue
                        current_status = await session.scalar(select(Task.status).where(Task.id == task_id))
                        if current_status in {"completed", "failed", "cancelled"}:
                            return
                except Exception:
                    logger.exception("Task lease heartbeat failed for %s", task_id)
                    self._requeue_on_cancel.add(task_id)
                if not execution_task.done():
                    execution_task.cancel()
                return
        except asyncio.CancelledError:
            return

    async def _cancel_requested_claims(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task.id).where(
                    Task.status == "running",
                    Task.claimed_by == self._worker_id,
                    Task.cancel_requested_at.is_not(None),
                )
            )
            task_ids = list(result.scalars().all())
        for task_id in task_ids:
            runner = self.active_tasks.get(task_id)
            if runner is not None:
                runner.cancel()

    async def _progress_notice_after(self, task_id: str, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if not task or task.status != "running":
                    return
                minutes = max(1, seconds // 60)
                body = (
                    f"⏳ Sigo trabajando en tu solicitud (lleva más de {minutes} min). Te envío el resultado por aquí apenas termine; "
                    "no hace falta reenviarla. Si prefieres detenerla, escribe «cancela»."
                )
                if await enqueue_progress_notice(session, task, body):
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # the heads-up is best-effort and must never touch the task
            logger.exception("Could not send the progress notice for task %s", task_id)

    def _claimed(self, task_id: str, claim_token: str, *, lock: bool = True):
        statement = select(Task).where(
            Task.id == task_id,
            Task.status == "running",
            Task.claim_token == claim_token,
        )
        return statement.with_for_update() if lock else statement

    async def _run_task(self, task_id: str, claim_token: str) -> None:
        callbacks: list = []
        task: Task | None = None
        try:
            conversation_history: list[dict] = []
            session_id: str | None = None
            async with self.session_factory() as session:
                task = (await session.execute(self._claimed(task_id, claim_token, lock=False))).scalar_one_or_none()
                if task is None:
                    return
                if task.cancel_requested_at is not None:
                    raise asyncio.CancelledError
                if not await self._schedule_still_authorized(session, task):
                    task.status = "cancelled"
                    await sync_board_with_runtime(session, task.id, task.status)
                    task.completed_at = utcnow()
                    task.error_message = (
                        "La programación fue deshabilitada, eliminada o perdió su aprobación antes de ejecutarse."
                    )
                    task.metadata_json = {**(task.metadata_json or {}), "end_reason": "programacion_no_vigente"}
                    self._release_claim(task)
                    await session.commit()
                    return
                agent = await session.get(Agent, task.agent_id)
                if not agent:
                    return
                if agent.status != "running":
                    task.status = "cancelled" if agent.is_archived else "queued"
                    task.started_at = None
                    task.completed_at = utcnow() if agent.is_archived else None
                    self._release_claim(task)
                    task.cancel_requested_at = None
                    if task.status == "queued":
                        task.attempt_count = max(0, task.attempt_count - 1)
                    await sync_board_with_runtime(session, task.id, task.status)
                    await session.commit()
                    self._dispatch_wakeup.set()
                    return
                task_owner_id = task.created_by_user_id
                task_audience = task_event_audience(task)
                conversation_history = await self._build_conversation_history(session, task)
                metadata = task.metadata_json or {}
                if metadata.get("conversation"):
                    candidate_session_id = conversation_key(metadata)
                    if candidate_session_id:
                        session_id = clave_sesion_runtime(candidate_session_id, metadata.get("conversation_epoch"))
                aviso_corte = aviso_de_corte_del_historial(conversation_history)
                if aviso_corte:
                    herramientas_previas = getattr(conversation_history, "herramientas", [])
                    omitidos_previos = (
                        getattr(conversation_history, "omitidos", 0),
                        getattr(conversation_history, "por_antiguedad", 0),
                    )
                    conversation_history = HistorialConversacion(
                        [
                            {"role": "user", "content": f"[Sistema, no es del usuario] {aviso_corte}"},
                            *conversation_history,
                        ]
                    )
                    conversation_history.herramientas = herramientas_previas
                    conversation_history.omitidos, conversation_history.por_antiguedad = omitidos_previos
                aviso_aprobacion = metadata.get("aprobacion_aviso")
                if aviso_aprobacion:
                    herramientas_previas = getattr(conversation_history, "herramientas", [])
                    conversation_history = HistorialConversacion(
                        [
                            *conversation_history,
                            {"role": "user", "content": f"[Sistema, no es del usuario] {aviso_aprobacion}"},
                        ]
                    )
                    conversation_history.herramientas = herramientas_previas

            event = {
                "type": "task.started",
                "task_id": task_id,
                "agent_id": task.agent_id,
            }
            if task_owner_id:
                event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(event, audience=task_audience)

            stream_buffer = _StreamBuffer(
                session_factory=self.session_factory,
                task_id=task_id,
                agent_id=task.agent_id,
                log_func=self._log,
                event_broker=self.event_broker,
                user_id=task_owner_id,
                claim_token=claim_token,
                audience=task_audience,
            )
            stream_buffer.start_flush_loop()

            async def stream_callback(delta: str, index: int | None = None) -> None:
                await stream_buffer.push(delta, index)

            progress_timer = asyncio.create_task(self._progress_notice_after(task_id, PROGRESS_NOTICE_SECONDS))
            try:
                runtime = self.runtimes.get(agent.runtime_type or "hermes", self.runtimes["hermes"])
                execution = await runtime.execute(
                    agent,
                    task,
                    stream_callback,
                    conversation_history=conversation_history,
                    session_id=session_id,
                )
            finally:
                progress_timer.cancel()
                await stream_buffer.stop_flush_loop()
            if execution.final_response is not None:
                execution.final_response = filtrar_respuesta(
                    task.prompt or "", execution.final_response or "", conversation_history
                )
            continuacion_id = None
            async with self.session_factory() as session:
                task = (await session.execute(self._claimed(task_id, claim_token))).scalar_one_or_none()
                agent = await session.get(Agent, task.agent_id) if task else None
                if not task or not agent:
                    logger.warning("Task %s: result discarded, the claim is no longer held", task_id)
                    return
                task.status = "completed"
                await sync_board_with_runtime(session, task.id, task.status)
                task.completed_at = utcnow()
                desglose_tiempos.adjuntar(task, self._workspace_de(agent))
                messages = getattr(execution, "messages", None)
                task.response = con_aviso_de_adjunto(
                    con_aviso_descarte_o_marcado_no_confirmado(
                        con_aviso_accion_graph_no_confirmada(
                            con_aviso_envio_no_confirmado(
                                con_borrador_canonico(
                                    con_informe_sin_lectura(
                                        con_tablas_de_correo(
                                            con_aviso_sin_herramienta(
                                                execution.final_response,
                                                len(execution.tool_calls or []),
                                                task.prompt,
                                            ),
                                            messages,
                                        ),
                                        messages,
                                    ),
                                    messages,
                                ),
                                messages,
                                task.prompt,
                            ),
                            messages,
                            aprobacion_ejecutada_en_servidor=MARCA_EJECUTADA
                            in str((task.metadata_json or {}).get("aprobacion_aviso") or ""),
                        ),
                        messages,
                    )
                )
                task.tokens_used = execution.tokens_used
                task.iterations = max(task.iterations, execution.iterations)
                task.messages_json = execution.messages or task.messages_json
                task.tool_calls = execution.tool_calls
                self._release_claim(task)
                task.cancel_requested_at = None

                metadata = dict(task.metadata_json or {})
                usage = getattr(execution, "usage", None)
                incompleto = motivo_resultado_incompleto(usage)
                metadata["end_reason"] = incompleto or "completed"
                if incompleto:
                    metadata["resultado_incompleto"] = True
                else:
                    metadata.pop("resultado_incompleto", None)
                if usage:
                    metadata["usage"] = usage
                for clave in ("exposicion", "compactacion"):
                    valor = getattr(execution, clave, None)
                    if isinstance(valor, dict) and valor:
                        metadata[clave] = valor
                for clave in ("accion_narrada", "generacion_desbocada", "verificacion"):
                    valor = getattr(execution, clave, None)
                    if isinstance(valor, dict) and valor:
                        metadata[clave] = valor
                    else:
                        metadata.pop(clave, None)
                correcciones = getattr(execution, "correcciones_aplicadas", None)
                if isinstance(correcciones, int) and correcciones:
                    metadata["correcciones_aplicadas"] = correcciones
                response_attachments = getattr(execution, "response_attachments", [])
                if response_attachments:
                    metadata["response_attachments"] = [
                        {k: v for k, v in att.items() if k != "source_path"} for att in response_attachments
                    ]
                if metadata.get("scheduled"):
                    try:
                        resumen_correo = referencias_correo.extraer_resumen(
                            task.response, messages, self._workspace_de(agent)
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("Could not store the mail list of scheduled summary %s", task_id)
                        resumen_correo = None
                    if resumen_correo:
                        metadata["resumen_correo"] = resumen_correo
                elif metadata.get("conversation") and not metadata.get("background_job"):
                    try:
                        listado_chat = referencias_correo.extraer_listado_chat(task.response, messages)
                    except Exception:  # noqa: BLE001
                        logger.exception("Could not store the mail list of chat task %s", task_id)
                        listado_chat = None
                    if listado_chat:
                        metadata[referencias_correo.CLAVE_LISTADO_CHAT] = listado_chat
                task.metadata_json = metadata

                agent.total_tasks += 1
                agent.total_tokens_used += task.tokens_used
                agent.last_activity = utcnow()
                await self._log(
                    session,
                    "task.completed",
                    agent=agent,
                    task=task,
                    message=task.title or "Task completed",
                    details={
                        "tokens_used": task.tokens_used,
                        "engine": execution.engine,
                        "resultado_incompleto": bool(incompleto),
                        "end_reason": metadata["end_reason"],
                    },
                )
                await self._queue_delegate_result_callback(
                    session,
                    task=task,
                    agent=agent,
                    success=True,
                    summary=execution.final_response or "",
                    callbacks=callbacks,
                )
                await self._queue_external_callback_delivery(
                    session,
                    task=task,
                    agent=agent,
                    success=True,
                    summary=execution.final_response or "",
                    callbacks=callbacks,
                )
                if incompleto:
                    try:
                        continuacion = await create_background_continuation(
                            session, task, error_de_incompleto(incompleto)
                        )
                        continuacion_id = continuacion.id if continuacion else None
                    except Exception:  # noqa: BLE001
                        logger.exception("Could not continue the incomplete task %s", task_id)
                if continuacion_id is None:
                    await enqueue_schedule_notification(
                        session, task, success=True, summary=task.response or execution.final_response
                    )
                await enqueue_delegation_notice(session, task, success=True, summary=execution.final_response)
                await enqueue_respuesta_tardia(session, task, success=True, summary=execution.final_response)
                await session.commit()
                await self._drain_callbacks(callbacks)
            if continuacion_id:
                await self.submit_task(continuacion_id)

            completed_event = {
                "type": "task.completed",
                "task_id": task_id,
                "agent_id": task.agent_id,
                "response": task.response or execution.final_response,
                "metadata": task.metadata_json or {},
            }
            if task_owner_id:
                completed_event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(completed_event, audience=task_audience)

            await self._run_post_task_hooks(task_id)

        except asyncio.CancelledError as cancel_exc:
            publish_event: dict | None = None
            audience = None
            async with self.session_factory() as session:
                task = (await session.execute(self._claimed(task_id, claim_token))).scalar_one_or_none()
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    requeue = task_id in self._requeue_on_cancel and task.cancel_requested_at is None
                    if requeue:
                        task.attempt_count = max(0, task.attempt_count - 1)
                        event_type, _ = await self._settle_interrupted(
                            session, task, utcnow(), "apagado_del_servicio", attempts_used=task.attempt_count
                        )
                    else:
                        task.status = "cancelled"
                        await sync_board_with_runtime(session, task.id, task.status)
                        task.completed_at = utcnow()
                        self._release_claim(task)
                        apply_failure_telemetry(task, cancel_exc, default_reason="cancelled")
                        desglose_tiempos.adjuntar(task, self._workspace_de(agent))
                        event_type = "task.cancelled"
                if task and agent:
                    agent.last_activity = utcnow()
                    await self._log(
                        session,
                        event_type,
                        agent=agent,
                        task=task,
                        message=task.title
                        or {"task.requeued": "Task requeued", "task.failed": "Task interrupted"}.get(
                            event_type, "Task cancelled"
                        ),
                    )
                await session.commit()
                if task and task.status in ("cancelled", "failed"):
                    publish_event = {
                        "type": "task.cancelled" if task.status == "cancelled" else "task.failed",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                    }
                    if task.status == "failed":
                        publish_event["error"] = task.error_message
                        publish_event["error_type"] = "Interrupted"
                    if task.created_by_user_id:
                        publish_event["created_by_user_id"] = task.created_by_user_id
                    audience = task_event_audience(task)
            if publish_event is not None:
                await self.event_broker.publish(publish_event, audience=audience)
        except (IdentityUnconfirmedError, ReconciliationRequiredError) as uncertain_exc:
            estado, motivo = (
                ("resultado_incierto", "identidad_no_confirmada")
                if isinstance(uncertain_exc, IdentityUnconfirmedError)
                else ("conciliacion_requerida", "acciones_externas_inciertas")
            )
            async with self.session_factory() as session:
                task = (await session.execute(self._claimed(task_id, claim_token))).scalar_one_or_none()
                if task:
                    task.status = "failed"
                    await sync_board_with_runtime(session, task.id, task.status)
                    task.completed_at = utcnow()
                    task.error_message = str(uncertain_exc)
                    self._release_claim(task)
                    task.metadata_json = {
                        **(task.metadata_json or {}),
                        "recovery": {"estado": estado, "motivo": motivo, "reintentable": False},
                    }
                    await session.commit()
        except ExecutionLeaseError as lease_exc:
            logger.warning(
                "Task %s: the execution attempt lost its lease; task left untouched (%s)", task_id, lease_exc
            )
        except Exception as exc:  # noqa: BLE001  # task cancellation catch-all
            failed_event: dict | None = None
            audience = None
            auto_job_id = None
            continuacion_id = None
            auto_job = None
            async with self.session_factory() as session:
                task = (await session.execute(self._claimed(task_id, claim_token))).scalar_one_or_none()
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "failed"
                    await sync_board_with_runtime(session, task.id, task.status)
                    task.completed_at = utcnow()
                    task.error_message = str(exc)
                    self._release_claim(task)
                    task.cancel_requested_at = None
                    apply_failure_telemetry(task, exc)
                    desglose_tiempos.adjuntar(task, self._workspace_de(agent))
                    try:
                        auto_job = await create_continuation(session, task, str(exc))
                        auto_job_id = auto_job.id if auto_job else None
                        if auto_job is not None:
                            handoff_title = (auto_job.metadata_json or {}).get("background_title") or task.title
                            await enqueue_handoff_notice(session, task, handoff_title)
                        else:
                            auto_job = await create_background_continuation(session, task, str(exc))
                            continuacion_id = auto_job.id if auto_job else None
                    except Exception:  # noqa: BLE001  # the continuation is best-effort
                        logger.exception("Could not start the automatic background continuation")
                    if auto_job_id is None:
                        await enqueue_respuesta_tardia(session, task, success=False, summary=str(exc))
                if task and agent:
                    agent.last_activity = utcnow()
                    await self._log(
                        session,
                        "task.failed",
                        agent=agent,
                        task=task,
                        message=task.title or "Task failed",
                        details={
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "traceback": traceback.format_exc(),
                        },
                    )
                    await self._queue_delegate_result_callback(
                        session,
                        task=task,
                        agent=agent,
                        success=False,
                        summary=str(exc),
                        callbacks=callbacks,
                    )
                    await self._queue_external_callback_delivery(
                        session,
                        task=task,
                        agent=agent,
                        success=False,
                        summary=str(exc),
                        callbacks=callbacks,
                    )
                    if auto_job is None:
                        await enqueue_schedule_notification(session, task, success=False, summary=str(exc))
                    await enqueue_delegation_notice(session, task, success=False, summary=str(exc))
                await session.commit()
                await self._drain_callbacks(callbacks)
                for nueva_id in (auto_job_id, continuacion_id):
                    if nueva_id:
                        await self.submit_task(nueva_id)
                if task:
                    failed_event = {
                        "type": "task.failed",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                    if auto_job_id:
                        failed_event["auto_background"] = True
                    if task.created_by_user_id:
                        failed_event["created_by_user_id"] = task.created_by_user_id
                    audience = task_event_audience(task)
            if failed_event is not None:
                await self.event_broker.publish(failed_event, audience=audience)

    async def _log(
        self,
        session: AsyncSession,
        event_type: str,
        *,
        agent: Agent | None = None,
        task: Task | None = None,
        node: Node | None = None,
        message: str | None = None,
        details: dict | None = None,
    ) -> None:
        session.add(
            ActivityLog(
                agent_id=agent.id if agent else None,
                task_id=task.id if task else None,
                node_id=node.id if node else agent.node_id if agent else None,
                event_type=event_type,
                message=message,
                details=details or {},
            )
        )

    async def _queue_delegate_result_callback(
        self,
        session: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        success: bool,
        summary: str,
        callbacks: list,
    ) -> None:
        if not task.source_agent_id:
            return
        if (task.metadata_json or {}).get("delegation_result"):
            return

        source_agent = await session.get(Agent, task.source_agent_id)
        if not source_agent:
            return

        status_label = "completed" if success else "failed"
        child_name = agent.friendly_name or agent.name or agent.slug or agent.id
        source_name = source_agent.friendly_name or source_agent.name or source_agent.slug or source_agent.id
        title = f"Delegation result from {child_name}"
        message_content = (
            f"Delegated task update from {child_name}: {status_label}.\n\n"
            f"Original instruction:\n{task.prompt}\n\n"
            f"Result:\n{summary.strip() or '(no response)'}"
        )

        callback_message = AgentMessage(
            from_agent_id=agent.id,
            to_agent_id=source_agent.id,
            task_id=task.id,
            message_type="delegate_result",
            content=message_content,
            metadata_json={
                "delegated_result": True,
                "status": status_label,
                "source_task_id": task.id,
                "parent_task_id": task.parent_task_id,
            },
        )
        session.add(callback_message)

        callback_task = Task(
            agent_id=source_agent.id,
            source_agent_id=agent.id,
            parent_task_id=task.parent_task_id,
            title=title,
            prompt=(
                f"A delegated task you assigned to {child_name} has {status_label}.\n\n"
                f"Original delegated instruction:\n{task.prompt}\n\n"
                f"{child_name} result:\n{summary.strip() or '(no response)'}\n\n"
                "If needed, continue the orchestration and inform the user."
            ),
            metadata_json={
                "delegation_result": True,
                "delegated_task_id": task.id,
                "delegated_agent_id": agent.id,
                "delegated_agent_name": child_name,
                "status": status_label,
                "callback_delivery": (task.metadata_json or {}).get("callback_delivery"),
            },
        )
        session.add(callback_task)
        await session.flush()
        callback_message.task_id = callback_task.id

        await self._log(
            session,
            "comms.delegate_result",
            agent=source_agent,
            task=callback_task,
            message=f"{child_name} -> {source_name}: delegated task {status_label}",
            details={
                "delegated_task_id": task.id,
                "delegated_agent_id": agent.id,
                "status": status_label,
            },
        )

        async def _after_commit() -> None:
            if source_agent.status == "running":
                await self.submit_task(callback_task.id)
            await self.event_broker.publish(
                {
                    "type": "comms.message",
                    "message_id": callback_message.id,
                    "from_agent_id": callback_message.from_agent_id,
                    "to_agent_id": callback_message.to_agent_id,
                    "message_type": callback_message.message_type,
                    "content": callback_message.content,
                    "task_id": callback_task.id,
                },
                audience=EventAudience.for_agents(agent.id, source_agent.id),
            )
            pty_manager = getattr(self, "pty_manager", None)
            if pty_manager is not None:
                notice = f"\r\n[HermesHQ] Delegation result from {child_name}: {status_label}. Task {task.id}\r\n"
                await pty_manager.broadcast_notice(source_agent.id, notice)

        callbacks.append(_after_commit)

    async def _queue_external_callback_delivery(
        self,
        session: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        success: bool,
        summary: str,
        callbacks: list,
    ) -> None:
        metadata = task.metadata_json or {}

        # ── Channel routing: skip external delivery for mobile_app tasks ──
        reply_to = str(metadata.get("reply_to") or metadata.get("source") or "").strip().lower()
        if reply_to == "mobile_app":
            return

        callback_delivery = metadata.get("callback_delivery")
        if not isinstance(callback_delivery, dict):
            return
        if not es_tarea_de_delegacion_del_servidor(task):
            logger.warning("callback_delivery ignorado en la tarea %s: no lo fijó una delegación del servidor", task.id)
            return
        platform = str(callback_delivery.get("platform") or "").strip().lower()
        chat_id = str(callback_delivery.get("chat_id") or "").strip()
        thread_id = callback_delivery.get("thread_id")
        if platform != "telegram" or not chat_id:
            return
        platform = str(callback_delivery.get("platform") or "").strip().lower()
        chat_id = str(callback_delivery.get("chat_id") or "").strip()
        thread_id = callback_delivery.get("thread_id")
        if platform != "telegram" or not chat_id:
            return

        message_text = summary.strip() if success else f"Delegated task failed: {summary.strip()}"
        if not message_text:
            return
        source_agent = await session.get(Agent, task.agent_id)
        if not source_agent:
            return
        result = await session.execute(
            select(MessagingChannel).where(
                MessagingChannel.agent_id == source_agent.id,
                MessagingChannel.platform == "telegram",
                MessagingChannel.enabled.is_(True),
            )
        )
        channel = result.scalar_one_or_none()
        if not channel or not channel.secret_ref:
            return
        secret_result = await session.execute(select(Secret).where(Secret.name == channel.secret_ref))
        secret = secret_result.scalar_one_or_none()
        if not secret:
            return
        token = self.secret_vault.decrypt(secret.value_enc)
        thread_value = int(str(thread_id)) if thread_id not in (None, "", "None") else None

        async def _after_commit() -> None:
            try:
                from telegram import Bot

                bot = Bot(token=token)
                await bot.send_message(chat_id=chat_id, text=message_text, message_thread_id=thread_value)
                await bot.shutdown()
            except Exception:  # noqa: BLE001  # telegram errors must never affect task state
                logger.warning("Failed to send Telegram notification to chat %s", chat_id, exc_info=True)

        callbacks.append(_after_commit)

    async def _drain_callbacks(self, callbacks: list) -> None:
        """Run post-commit callbacks one by one; a failing callback never
        aborts the rest nor propagates into task state handling."""
        for callback in callbacks:
            try:
                await callback()
            except Exception:  # noqa: BLE001  # post-commit side effect — log and continue
                logger.exception("Post-commit task callback failed")

    async def get_recent_activity(self, limit: int = 20) -> list[ActivityLog]:
        async with self.session_factory() as session:
            result = await session.execute(select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit))
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Post-task hooks
    # ------------------------------------------------------------------

    async def _run_post_task_hooks(self, task_id: str) -> None:
        """Run post-completion hooks based on task metadata."""
        try:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if not task:
                    return
                metadata = task.metadata_json or {}

                # Avatar generation hook
                if metadata.get("avatar_generation"):
                    target_agent_id = metadata.get("target_agent_id")
                    if target_agent_id:
                        await self._apply_avatar_generation(session, task, target_agent_id)
        except Exception as exc:  # noqa: BLE001  # post-task hook best-effort
            import logging

            logging.getLogger(__name__).warning("Post-task hook failed for %s: %s", task_id, exc)

    async def _apply_avatar_generation(
        self,
        session: AsyncSession,
        task: Task,
        target_agent_id: str,
    ) -> None:
        """Find generated image in operator workspace and apply as avatar.

        Uses the avatar service layer (save_avatar_bytes) instead of
        writing directly to the database or filesystem.
        """
        from pathlib import Path

        from hermeshq.config import get_settings
        from hermeshq.services.avatar import AVATAR_MEDIA_TYPES, delete_avatar_files, save_avatar_bytes

        settings = get_settings()

        # Operator workspace: work/ directory where hermes-agent saves files
        operator_workspace = Path(settings.workspaces_root) / f"agent-{task.agent_id}" / "work"
        if not operator_workspace.exists():
            return

        # Find image files (png, jpg, jpeg, webp, svg) sorted newest first
        image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        candidates = sorted(
            [f for f in operator_workspace.rglob("*") if f.is_file() and f.suffix.lower() in image_extensions],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return

        # Take the most recent image
        source = candidates[0]

        # Resolve content type from extension
        ext = source.suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        content_type = AVATAR_MEDIA_TYPES.get(ext, "image/png")

        # Use the avatar service layer to save
        avatar_base = (
            Path(settings.agent_assets_root)
            if settings.agent_assets_root
            else Path(settings.workspaces_root) / "_agent_assets"
        )
        content = source.read_bytes()
        filename = save_avatar_bytes(avatar_base, target_agent_id, content, content_type)

        # Update target agent in DB
        target_agent = await session.get(Agent, target_agent_id)
        if target_agent:
            target_agent.avatar_filename = filename
            try:
                await session.commit()
            except Exception:
                delete_avatar_files(avatar_base, target_agent_id)
                raise

            await self._log(
                session,
                "agent.avatar.generated",
                agent=target_agent,
                task=task,
                message="AI avatar applied from operator task",
            )
            await session.commit()
            await self.event_broker.publish(
                {
                    "type": "agent.avatar_updated",
                    "agent_id": target_agent_id,
                },
                audience=EventAudience.for_agent(target_agent_id),
            )


# ---------------------------------------------------------------------------
# Module-level helper to get the running supervisor from the FastAPI app.
# ---------------------------------------------------------------------------


def get_supervisor() -> AgentSupervisor:
    """Return the AgentSupervisor attached to the running FastAPI app state."""
    # Lazy import to avoid circular dependency at module level.
    from hermeshq.main import app  # noqa: WPS433

    supervisor: AgentSupervisor | None = getattr(app.state, "supervisor", None)
    if supervisor is None:
        raise RuntimeError("AgentSupervisor not initialised yet")
    return supervisor
