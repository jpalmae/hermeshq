import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, update
from test_e2e_reset_aislamiento import _clave_publica, stack_reset  # noqa: F401

from hermeshq.models.conversation_thread import ConversationThread
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

CLAVE_USUARIOS = "clave-solo-pruebas"
CLAVES_INTERNAS = (
    "scheduled",
    "scheduled_task_id",
    "manual_trigger",
    "delegated",
    "delegation_owner_user_id",
    "identity_approval_code",
    "callback_delivery",
    "background_job",
    "background_title",
    "aprobacion_id",
    "public_session_id",
    "response_attachments",
    "conversation_epoch",
    "reset",
    "respuesta_tardia",
)


async def _usuario(maker, agent_id: str, nombre: str) -> tuple[str, str]:
    from hermeshq.core.security import hash_password
    from hermeshq.models.agent_assignment import AgentAssignment
    from hermeshq.models.user import User

    username = f"{nombre}-{uuid.uuid4().hex[:6]}"
    async with maker() as s:
        usuario = User(username=username, display_name=nombre, password_hash=hash_password(CLAVE_USUARIOS), role="user")
        s.add(usuario)
        await s.flush()
        s.add(AgentAssignment(user_id=usuario.id, agent_id=agent_id))
        await s.commit()
        return usuario.id, username


async def _cabeceras(base: str, username: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/api/auth/login", json={"username": username, "password": CLAVE_USUARIOS})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _crear_tarea(base: str, headers: dict, cuerpo: dict) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/api/tasks", headers=headers, json=cuerpo, timeout=15)
    assert r.status_code == 201, r.text
    return r.json()


async def _esperar_terminadas(maker, ids: list[str], timeout: float = 150.0) -> list[Task]:
    limite = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < limite:
        async with maker() as s:
            filas = (await s.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        if len(filas) == len(ids) and all(f.status in ("completed", "failed", "cancelled") for f in filas):
            return filas
        await asyncio.sleep(0.5)
    raise AssertionError("las tareas no terminaron")


class TestMetadataDeClienteEntreUsuarios:
    async def test_usuario_b_no_desvia_respuestas_al_cron_ni_a_la_delegacion_de_a(self, stack_reset):  # noqa: F811
        maker, agent_id = stack_reset["maker"], stack_reset["agent_id"]
        instancia = stack_reset["spawn"]("instancia-metadata")
        await instancia.wait_ready()
        id_a, _ = await _usuario(maker, agent_id, "usuario-a")
        id_b, nombre_b = await _usuario(maker, agent_id, "usuario-b")
        await _clave_publica(maker, agent_id, id_a)
        async with maker() as s:
            await s.execute(
                update(PublicChatApiKey)
                .where(PublicChatApiKey.bound_user_id == id_a)
                .values(last_notification_poll_at=datetime.now(UTC))
            )
            cron = ScheduledTask(
                agent_id=agent_id,
                name="informe-de-a",
                cron_expression="0 9 * * 1",
                prompt="Informe privado de A",
                enabled=True,
                next_run=datetime.now(UTC) + timedelta(days=30),
                owner_user_id=id_a,
                notify_public_chat=True,
                approval_status="approved",
            )
            hilo_a = ConversationThread(agent_id=agent_id, user_id=id_a, title="Chat de A")
            s.add_all([cron, hilo_a])
            await s.commit()
            cron_id, hilo_a_id = cron.id, hilo_a.id

        headers_b = await _cabeceras(instancia.base, nombre_b)
        maliciosas = [
            {"scheduled": True, "scheduled_task_id": cron_id, "thread_user_id": id_a},
            {"delegated": True, "delegation_owner_user_id": id_a, "identity_approval_code": "ABC123"},
            {
                "callback_delivery": {"platform": "telegram", "chat_id": "999"},
                "thread_id": hilo_a_id,
                "source": "public_chat",
                "public_session_id": "sesion-de-a",
                "background_job": True,
                "background_title": "x",
                "respuesta_tardia": True,
                "reset": True,
                "conversation_epoch": 9,
            },
        ]
        creadas = []
        for indice, metadata in enumerate(maliciosas):
            creadas.append(
                await _crear_tarea(
                    instancia.base,
                    headers_b,
                    {"agent_id": agent_id, "prompt": f"Respuesta privada de B MARCA-B-{indice}", "metadata": metadata},
                )
            )
        legitima = await _crear_tarea(
            instancia.base,
            headers_b,
            {
                "agent_id": agent_id,
                "title": "Chat message",
                "prompt": "Hola MARCA-B-LEGITIMA",
                "metadata": {"conversation": True, "source": "agent_conversation", "thread_id": f"console_{agent_id}"},
            },
        )

        for tarea in creadas:
            metadata = tarea["metadata"]
            assert not set(CLAVES_INTERNAS) & set(metadata), metadata
            assert metadata["thread_user_id"] == id_b
            assert metadata.get("thread_id") != hilo_a_id
            assert metadata.get("source") != "public_chat"
        assert legitima["metadata"]["conversation"] is True
        assert legitima["metadata"]["source"] == "agent_conversation"
        assert legitima["metadata"]["thread_id"] not in (hilo_a_id, f"console_{agent_id}")

        filas = await _esperar_terminadas(maker, [t["id"] for t in creadas] + [legitima["id"]])
        for fila in filas:
            assert not set(CLAVES_INTERNAS) & set(fila.metadata_json or {}), fila.metadata_json
            assert fila.created_by_user_id == id_b
        async with maker() as s:
            consulta = select(PublicChatNotification).where(PublicChatNotification.recipient_user_id == id_a)
            avisos = (await s.execute(consulta)).scalars().all()
            hilo_b = (
                await s.execute(
                    select(ConversationThread).where(
                        ConversationThread.agent_id == agent_id, ConversationThread.user_id == id_b
                    )
                )
            ).scalar_one()
        assert avisos == [], [(a.title, a.body[:80]) for a in avisos]
        assert legitima["metadata"]["thread_id"] == hilo_b.id
