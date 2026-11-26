import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.base import utcnow
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers import public_chat as router_chat
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.public_chat_service import PublicChatService

pytestmark = [pytest.mark.integration, requires_database]

PEDIDO = "Prepara el resumen de ventas de la semana para el comité de gerencia"
RESPUESTA = "Listo el resumen de ventas. ¿Confirmas que lo envíe al comité?"


@pytest.fixture
async def entorno(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        node = Node(name=f"nodo-{uuid.uuid4().hex[:6]}", hostname="localhost")
        db.add(node)
        await db.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-cx1",
            slug=f"agente-cx1-{uuid.uuid4().hex[:6]}",
            workspace_path="/tmp/ws/cx1",
            status="running",
            model="modelo-simulado",
            enabled_toolsets=[],
        )
        db.add(agent)
        await db.flush()
        usuarios = {}
        for nombre, teams in (("ana", "29:ana"), ("beto", "29:beto")):
            user = User(
                username=f"{nombre}-{uuid.uuid4().hex[:6]}",
                display_name=nombre,
                password_hash="x",
                teams_id=teams,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            db.add(AgentAssignment(user_id=user.id, agent_id=agent.id))
            usuarios[nombre] = user.id
        await db.commit()
        agent_id = agent.id
    servicio = PublicChatService(maker, SimpleNamespace(submit_task=AsyncMock(), active_tasks={}), MagicMock())
    return SimpleNamespace(maker=maker, agent_id=agent_id, usuarios=usuarios, servicio=servicio)


async def _clave(entorno, *, trust_sender_id=False, bound_user_id=None) -> PublicChatApiKey:
    async with entorno.maker() as db:
        clave = PublicChatApiKey(
            key_hash=uuid.uuid4().hex,
            key_prefix="pk_test",
            label="teams-cx1",
            agent_id=entorno.agent_id,
            allowed_domains=["*"],
            trust_sender_id=trust_sender_id,
            bound_user_id=bound_user_id,
        )
        db.add(clave)
        await db.commit()
        return clave


async def _crear_sesion(entorno, clave, sender_id=None) -> dict:
    async with entorno.maker() as db:
        return await entorno.servicio.create_session(await db.get(PublicChatApiKey, clave.id), db, sender_id=sender_id)


async def _enviar(entorno, sesion: dict, contenido: str) -> str:
    async with entorno.maker() as db:
        task_id, _ = await entorno.servicio.send_message(sesion["session_id"], sesion["session_token"], contenido, db)
        return task_id


async def _sql(entorno, sentencia: str, **params) -> None:
    async with entorno.maker() as db:
        await db.execute(text(sentencia), params)
        await db.commit()


async def _tarea_larga_terminada(entorno, sesion: dict, minutos: int) -> str:
    task_id = await _enviar(entorno, sesion, PEDIDO)
    await _sql(
        entorno,
        "UPDATE public_chat_sessions SET last_activity = now() - make_interval(mins => :m) WHERE id = :s",
        m=minutos,
        s=sesion["session_id"],
    )
    await _sql(
        entorno,
        "UPDATE tasks SET status = 'completed', response = :r, queued_at = now() - make_interval(mins => :m), "
        "completed_at = now() WHERE id = :t",
        r=RESPUESTA,
        m=minutos,
        t=task_id,
    )
    return task_id


async def _pasan_minutos(entorno, session_id: str, minutos: int) -> None:
    await _sql(
        entorno,
        "UPDATE public_chat_sessions SET last_activity = last_activity - make_interval(mins => :m) WHERE id = :s",
        m=minutos,
        s=session_id,
    )


async def _historial(entorno, task_id: str) -> str:
    async with entorno.maker() as db:
        tarea = await db.get(Task, task_id)
        historial = await AgentSupervisor.__new__(AgentSupervisor)._build_conversation_history(db, tarea)
    return "\n".join(str(m.get("content") or "") for m in historial)


async def _caducar(entorno, session_id: str, minutos: int) -> None:
    await _sql(
        entorno,
        "UPDATE public_chat_sessions SET status = 'expired', last_activity = now() - make_interval(mins => :m) "
        "WHERE id = :s",
        m=minutos,
        s=session_id,
    )


class TestActividadRenovadaPorElAgente:
    async def test_tarea_de_11_minutos_y_respuesta_al_minuto_conserva_el_historial(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await entorno.servicio.save_assistant_message(sesion["session_id"], RESPUESTA)
        await _pasan_minutos(entorno, sesion["session_id"], 1)

        confirmo = await _enviar(entorno, sesion, "confirmo")

        historial = await _historial(entorno, confirmo)
        assert PEDIDO in historial
        assert RESPUESTA in historial

    async def test_entrega_tardia_por_notificacion_renueva_la_sesion(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        task_id = await _tarea_larga_terminada(entorno, sesion, minutos=9)
        async with entorno.maker() as db:
            notificacion = PublicChatNotification(
                api_key_id=clave.id, agent_id=entorno.agent_id, task_id=task_id, title="Resultado", body=RESPUESTA
            )
            db.add(notificacion)
            await db.commit()
            await router_chat._renovar_sesion_de_la_entrega(entorno.servicio, db, notificacion, clave.id)
            await db.commit()
        await _pasan_minutos(entorno, sesion["session_id"], 2)

        confirmo = await _enviar(entorno, sesion, "confirmo")

        assert PEDIDO in await _historial(entorno, confirmo)

    async def test_la_entrega_de_otra_clave_no_renueva(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        ajena = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        task_id = await _tarea_larga_terminada(entorno, sesion, minutos=9)
        async with entorno.maker() as db:
            notificacion = SimpleNamespace(id="n-ajena", task_id=task_id)
            await router_chat._renovar_sesion_de_la_entrega(entorno.servicio, db, notificacion, ajena.id)
            await db.commit()
            fila = await db.get(PublicChatSession, sesion["session_id"])
            assert utcnow() - fila.last_activity > timedelta(minutes=8)


class TestSesionNuevaHeredaLaConversacion:
    async def test_sesion_caducada_del_mismo_usuario_se_retoma_con_su_historial(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await _caducar(entorno, sesion["session_id"], 20)

        nueva = await _crear_sesion(entorno, clave, "29:ana")
        confirmo = await _enviar(entorno, nueva, "confirmo")

        assert nueva["session_id"] == sesion["session_id"]
        assert nueva["session_token"] != sesion["session_token"]
        historial = await _historial(entorno, confirmo)
        assert PEDIDO in historial
        with pytest.raises(ValueError):
            await _enviar(entorno, sesion, "con el token viejo")

    async def test_clave_con_usuario_fijo_tambien_retoma(self, entorno):
        clave = await _clave(entorno, bound_user_id=entorno.usuarios["ana"])
        sesion = await _crear_sesion(entorno, clave)
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await _caducar(entorno, sesion["session_id"], 15)

        nueva = await _crear_sesion(entorno, clave)

        assert nueva["session_id"] == sesion["session_id"]

    async def test_reset_corta_y_no_se_hereda(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await entorno.servicio.save_assistant_message(sesion["session_id"], RESPUESTA)
        async with entorno.maker() as db:
            respuesta = await entorno.servicio.handle_cancel_request(
                sesion["session_id"], sesion["session_token"], "/reset", db
            )
        assert respuesta and "reiniciada" in respuesta
        await _caducar(entorno, sesion["session_id"], 20)

        nueva = await _crear_sesion(entorno, clave, "29:ana")
        confirmo = await _enviar(entorno, nueva, "confirmo")

        assert nueva["session_id"] != sesion["session_id"]
        assert PEDIDO not in await _historial(entorno, confirmo)

    async def test_otro_usuario_no_hereda(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await _caducar(entorno, sesion["session_id"], 20)

        de_beto = await _crear_sesion(entorno, clave, "29:beto")
        mensaje = await _enviar(entorno, de_beto, "confirmo")

        assert de_beto["session_id"] != sesion["session_id"]
        historial = await _historial(entorno, mensaje)
        assert PEDIDO not in historial
        assert RESPUESTA not in historial

    async def test_si_cambia_el_usuario_fijo_de_la_clave_no_hereda(self, entorno):
        clave = await _clave(entorno, bound_user_id=entorno.usuarios["ana"])
        sesion = await _crear_sesion(entorno, clave)
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await _caducar(entorno, sesion["session_id"], 15)
        await _sql(
            entorno,
            "UPDATE public_chat_api_keys SET bound_user_id = :u WHERE id = :k",
            u=entorno.usuarios["beto"],
            k=clave.id,
        )

        nueva = await _crear_sesion(entorno, clave)

        assert nueva["session_id"] != sesion["session_id"]

    async def test_chat_anonimo_nunca_hereda(self, entorno):
        clave = await _clave(entorno)
        sesion = await _crear_sesion(entorno, clave)
        await _tarea_larga_terminada(entorno, sesion, minutos=11)
        await _caducar(entorno, sesion["session_id"], 15)

        nueva = await _crear_sesion(entorno, clave)

        assert nueva["session_id"] != sesion["session_id"]

    async def test_fuera_de_la_ventana_o_cerrada_no_hereda(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        vieja = await _crear_sesion(entorno, clave, "29:ana")
        await _tarea_larga_terminada(entorno, vieja, minutos=11)
        await _caducar(entorno, vieja["session_id"], 70)
        assert (await _crear_sesion(entorno, clave, "29:ana"))["session_id"] != vieja["session_id"]

        clave2 = await _clave(entorno, trust_sender_id=True)
        cerrada = await _crear_sesion(entorno, clave2, "29:ana")
        await _tarea_larga_terminada(entorno, cerrada, minutos=11)
        async with entorno.maker() as db:
            await entorno.servicio.close_session(cerrada["session_id"], cerrada["session_token"], db)
        assert (await _crear_sesion(entorno, clave2, "29:ana"))["session_id"] != cerrada["session_id"]

    async def test_sesion_vigente_no_se_reutiliza(self, entorno):
        clave = await _clave(entorno, trust_sender_id=True)
        sesion = await _crear_sesion(entorno, clave, "29:ana")
        await _enviar(entorno, sesion, PEDIDO)

        nueva = await _crear_sesion(entorno, clave, "29:ana")

        assert nueva["session_id"] != sesion["session_id"]
        async with entorno.maker() as db:
            vigente = await db.execute(
                select(PublicChatSession.status).where(PublicChatSession.id == sesion["session_id"])
            )
            assert vigente.scalar_one() == "active"
