from __future__ import annotations

import uuid

import pytest
from conftest import requires_database

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.bot_channel import BotChannel
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers.attachments import _tarea_de_chat_publico_referencia

pytestmark = [pytest.mark.integration, requires_database]


async def _escenario(db, tmp_path, canal_propio_del_segundo_bot: bool = False):
    sufijo = uuid.uuid4().hex[:6]
    nodo = Node(name=f"nodo-{sufijo}", hostname="localhost")
    db.add(nodo)
    primero = User(username="bridge-teams", display_name="b1", password_hash="x", role="user")
    segundo = User(username="bridge-teams-segundo_bot", display_name="b2", password_hash="x", role="user")
    db.add_all([primero, segundo])
    await db.flush()
    a1 = Agent(
        node_id=nodo.id, name=f"a1-{sufijo}", slug=f"a1-{sufijo}", workspace_path=str(tmp_path / "a1"), status="running"
    )
    a2 = Agent(
        node_id=nodo.id, name=f"a2-{sufijo}", slug=f"a2-{sufijo}", workspace_path=str(tmp_path / "a2"), status="running"
    )
    db.add_all([a1, a2])
    await db.flush()
    db.add_all(
        [
            AgentAssignment(user_id=primero.id, agent_id=a1.id),
            AgentAssignment(user_id=segundo.id, agent_id=a2.id),
        ]
    )
    defecto = BotChannel(
        slug=f"principal-{sufijo}", display_name="Principal", service_user_id=primero.id, is_default=True
    )
    db.add(defecto)
    canal2 = None
    if canal_propio_del_segundo_bot:
        canal2 = BotChannel(slug=f"segundo-{sufijo}", display_name="Segundo", service_user_id=segundo.id)
        db.add(canal2)
    await db.flush()
    return primero, segundo, a1, a2, defecto, canal2


def _tarea(agente, file_id, canal=None):
    metadata = {"source": "public_chat", "response_attachments": [{"file_id": file_id, "filename": "i.html"}]}
    if canal:
        metadata["bot_channel_id"] = canal
    return Task(agent_id=agente.id, prompt="x", status="completed", metadata_json=metadata)


FID = "7dbff80a-bd59-423f-bdb9-f7def3b94774"
OTRO = "11111111-2222-4333-8444-555555555555"


async def test_caso_real_el_segundo_bot_descarga_los_archivos_de_su_agente(db_session, tmp_path):
    primero, segundo, a1, a2, _, _ = await _escenario(db_session, tmp_path)
    db_session.add(_tarea(a2, FID))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a2.id, FID) is True


async def test_el_primer_bot_sigue_descargando_lo_suyo_sin_canal(db_session, tmp_path):
    primero, segundo, a1, a2, _, _ = await _escenario(db_session, tmp_path)
    db_session.add(_tarea(a1, FID))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, primero.id, a1.id, FID) is True


async def test_el_segundo_bot_no_descarga_archivos_de_un_agente_que_atiende_el_canal_por_defecto(db_session, tmp_path):
    primero, segundo, a1, a2, _, _ = await _escenario(db_session, tmp_path)
    db_session.add(_tarea(a1, FID))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a1.id, FID) is False


async def test_un_archivo_que_la_tarea_no_referencia_no_se_entrega(db_session, tmp_path):
    primero, segundo, a1, a2, _, _ = await _escenario(db_session, tmp_path)
    db_session.add(_tarea(a2, FID))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a2.id, OTRO) is False


async def test_una_tarea_de_otro_origen_no_se_entrega(db_session, tmp_path):
    primero, segundo, a1, a2, _, _ = await _escenario(db_session, tmp_path)
    db_session.add(
        Task(
            agent_id=a2.id,
            prompt="x",
            status="completed",
            metadata_json={"source": "api", "response_attachments": [{"file_id": FID}]},
        )
    )
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a2.id, FID) is False


async def test_con_canal_explicito_de_otro_bot_el_segundo_no_lo_descarga(db_session, tmp_path):
    primero, segundo, a1, a2, defecto, canal2 = await _escenario(
        db_session, tmp_path, canal_propio_del_segundo_bot=True
    )
    db_session.add(_tarea(a2, FID, canal=defecto.id))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a2.id, FID) is False


async def test_con_su_propio_canal_registrado_el_segundo_bot_descarga(db_session, tmp_path):
    primero, segundo, a1, a2, defecto, canal2 = await _escenario(
        db_session, tmp_path, canal_propio_del_segundo_bot=True
    )
    db_session.add(_tarea(a2, FID, canal=canal2.id))
    await db_session.flush()
    assert await _tarea_de_chat_publico_referencia(db_session, segundo.id, a2.id, FID) is True
