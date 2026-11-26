from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import requires_database, run_queued
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _seed_task

from hermeshq.models.agent import Agent
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services import auto_background
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.hermes_runtime import RuntimeExecutionError, RuntimeExecutionResult
from hermeshq.services.schedule_notifications import enqueue_respuesta_tardia, enqueue_schedule_notification

MAX_ITER = "max_iterations_reached(120/120)"
SIN_SALIDA = "Hermes runtime produced no output for 600s and was stopped"


class TestMotivoIncompleto:
    def test_reconoce_los_cortes_reales_del_motor(self):
        assert auto_background.motivo_resultado_incompleto({"turn_exit_reason": MAX_ITER}) == "max_iterations"
        assert auto_background.motivo_resultado_incompleto({"turn_exit_reason": "guardrail_halt"}) == "guardrail"

    def test_un_turno_normal_no_es_incompleto(self):
        normal = {"turn_exit_reason": "text_response(finish_reason=stop)"}
        assert auto_background.motivo_resultado_incompleto(normal) is None
        assert auto_background.motivo_resultado_incompleto(None) is None
        assert auto_background.motivo_resultado_incompleto({}) is None


def _bg(**extra):
    metadata = {"background_job": True, "thread_user_id": "u1", "background_title": "Informe HSE", **extra}
    return SimpleNamespace(id="t1", agent_id="a1", prompt="informe largo", metadata_json=metadata)


def _chat(**extra):
    metadata = {"source": "public_chat", "thread_user_id": "u1", "public_session_id": "s1", **extra}
    return SimpleNamespace(id="t1", agent_id="a1", prompt="arma el informe", metadata_json=metadata)


class TestContinuabilidad:
    def test_un_trabajo_incompleto_en_segundo_plano_se_continua(self):
        error = auto_background.error_de_incompleto("max_iterations")
        assert auto_background.is_background_continuable(_bg(), error)
        assert not auto_background.is_background_continuable(_bg(continuaciones=2), error)

    def test_un_chat_incompleto_no_se_manda_a_segundo_plano(self):
        assert not auto_background.is_continuable(_chat(), auto_background.error_de_incompleto("guardrail"))

    def test_sin_salida_solo_se_continua_sin_acciones_externas(self):
        assert not auto_background.is_continuable(_chat(), SIN_SALIDA)
        assert auto_background.is_continuable(_chat(), SIN_SALIDA, sin_acciones_externas=True)
        assert not auto_background.is_background_continuable(_bg(), SIN_SALIDA)
        assert auto_background.is_background_continuable(_bg(), SIN_SALIDA, sin_acciones_externas=True)

    def test_el_prompt_explica_el_motivo_real_del_corte(self):
        guardrail = auto_background.prompt_continuacion_background(
            "x", 2, 60, auto_background.error_de_incompleto("guardrail")
        )
        assert "falló varias veces seguidas" in guardrail
        assert "límite de tiempo" not in guardrail
        assert "límite de tiempo" in auto_background.prompt_continuacion_background("x", 2, 60)


def _sesion_continuacion(acciones: int):
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id="u1", is_active=True))
    conteo = MagicMock()
    conteo.scalar.return_value = acciones
    activos = MagicMock()
    activos.scalars.return_value.all.return_value = []
    historial = MagicMock()
    historial.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[conteo, activos, historial])
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestSinSalidaConAccionesExternas:
    async def test_no_repite_si_la_tarea_ya_hizo_una_accion_externa(self):
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            assert await auto_background.create_continuation(_sesion_continuacion(1), _chat(), SIN_SALIDA) is None
            assert (
                await auto_background.create_background_continuation(_sesion_continuacion(1), _bg(), SIN_SALIDA) is None
            )

    async def test_continua_si_no_hubo_acciones_externas(self):
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            job = await auto_background.create_continuation(_sesion_continuacion(0), _chat(), SIN_SALIDA)
            assert job is not None and job.metadata_json["background_job"]
            parte = await auto_background.create_background_continuation(_sesion_continuacion(0), _bg(), SIN_SALIDA)
            assert parte is not None and parte.metadata_json["continuaciones"] == 1

    async def test_la_marca_de_acciones_en_metadata_basta_para_no_continuar(self):
        session = _sesion_continuacion(0)
        tarea = _chat(acciones_externas=[{"tool": "enviar_correo"}])
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            assert await auto_background.create_continuation(session, tarea, SIN_SALIDA) is None


def _sesion_aviso():
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id="u1", is_active=True, teams_id=None))
    llaves = MagicMock()
    llaves.scalars.return_value.all.return_value = [
        SimpleNamespace(id="k1", trust_sender_id=False, last_notification_poll_at=datetime.now(UTC))
    ]
    session.execute = AsyncMock(return_value=llaves)
    session.add = MagicMock()
    return session


class TestAvisosDeResultadoIncompleto:
    async def test_segundo_plano_incompleto_no_dice_que_termino(self):
        session = _sesion_aviso()
        tarea = _bg(resultado_incompleto=True, end_reason="max_iterations")
        await enqueue_schedule_notification(session, tarea, success=True, summary="avance parcial")
        cuerpo = session.add.call_args.args[0].body
        assert not cuerpo.startswith("✅")
        assert "Terminó" not in cuerpo
        assert "quedó incompleto" in cuerpo and "límite de pasos" in cuerpo
        assert "avance parcial" in cuerpo

    async def test_segundo_plano_completo_sigue_igual(self):
        session = _sesion_aviso()
        await enqueue_schedule_notification(session, _bg(end_reason="completed"), success=True, summary="listo")
        assert session.add.call_args.args[0].body.startswith("✅ Terminó")

    async def test_respuesta_tardia_incompleta_lo_advierte(self):
        session = _sesion_aviso()
        tarea = _chat(respuesta_tardia=True, resultado_incompleto=True, end_reason="guardrail")
        await enqueue_respuesta_tardia(session, tarea, success=True, summary="parcial")
        cuerpo = session.add.call_args.args[0].body
        assert cuerpo.startswith("⚠️ No alcancé a terminar")
        assert "herramienta falló" in cuerpo


def _maker(db_engine):
    engine, _ = db_engine
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _preparar(maker, metadata: dict, con_llave: bool = True) -> tuple[str, str]:
    agent_id, _ = await _seed_task(maker)
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        agent.status = "running"
        user = User(username=f"u-{uuid.uuid4().hex[:8]}", display_name="Usuaria", password_hash="x", is_active=True)
        session.add(user)
        await session.flush()
        if con_llave:
            session.add(
                PublicChatApiKey(
                    key_hash=uuid.uuid4().hex,
                    key_prefix="hq_x",
                    label="bridge",
                    agent_id=agent_id,
                    bound_user_id=user.id,
                    last_notification_poll_at=datetime.now(UTC),
                )
            )
        meta = {**metadata}
        if meta.get("thread_user_id") == "@":
            meta["thread_user_id"] = user.id
        task = Task(agent_id=agent_id, prompt="Genera el informe por rol", status="queued", metadata_json=meta)
        session.add(task)
        await session.commit()
        return agent_id, task.id


def _resultado(turn_exit_reason: str) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        final_response="Avance: revisé 3 de 9 roles.",
        messages=[],
        tool_calls=[],
        tokens_used=10,
        iterations=120,
        engine="stub",
        response_attachments=[],
        usage={"turn_exit_reason": turn_exit_reason, "api_calls": 120},
    )


def _supervisor(maker, *, resultado=None, error=None):
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=resultado, side_effect=error)
    supervisor = AgentSupervisor(maker, MagicMock(publish=AsyncMock()), runtime, secret_vault=None)
    supervisor.submit_task = AsyncMock()
    return supervisor


async def _tareas(maker, agent_id):
    async with maker() as session:
        return (await session.execute(select(Task).where(Task.agent_id == agent_id))).scalars().all()


async def _avisos(maker, agent_id):
    async with maker() as session:
        filas = await session.execute(select(PublicChatNotification).where(PublicChatNotification.agent_id == agent_id))
        return filas.scalars().all()


@pytest.mark.integration
@requires_database
class TestRunTaskConRunnerFalso:
    async def test_segundo_plano_que_agota_iteraciones_queda_incompleto_y_continua(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(
            maker, {"background_job": True, "thread_user_id": "@", "background_title": "Informe por rol"}
        )
        supervisor = _supervisor(maker, resultado=_resultado(MAX_ITER))
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            await run_queued(supervisor, task_id)
        tareas = {t.id: t for t in await _tareas(maker, agent_id)}
        tarea = tareas[task_id]
        assert tarea.status == "completed"
        assert tarea.metadata_json["end_reason"] == "max_iterations"
        assert tarea.metadata_json["resultado_incompleto"] is True
        nuevas = [t for t in tareas.values() if (t.metadata_json or {}).get("origin_task_id") == task_id]
        assert len(nuevas) == 1 and nuevas[0].status == "queued"
        assert nuevas[0].metadata_json["continuaciones"] == 1
        assert "límite de pasos" in nuevas[0].prompt
        supervisor.submit_task.assert_awaited_once_with(nuevas[0].id)
        assert not any((a.body or "").startswith("✅") for a in await _avisos(maker, agent_id))

    async def test_sin_continuacion_posible_avisa_que_quedo_incompleto(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(
            maker,
            {"background_job": True, "thread_user_id": "@", "background_title": "Informe", "continuaciones": 2},
        )
        supervisor = _supervisor(maker, resultado=_resultado("guardrail_halt"))
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            await run_queued(supervisor, task_id)
        avisos = await _avisos(maker, agent_id)
        assert len(avisos) == 1
        assert avisos[0].body.startswith("⚠️") and "quedó incompleto" in avisos[0].body
        assert "Terminó" not in avisos[0].body
        supervisor.submit_task.assert_not_awaited()
        tarea = {t.id: t for t in await _tareas(maker, agent_id)}[task_id]
        assert tarea.metadata_json["end_reason"] == "guardrail"

    async def test_turno_normal_sigue_completed(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(
            maker, {"background_job": True, "thread_user_id": "@", "background_title": "Informe"}
        )
        supervisor = _supervisor(maker, resultado=_resultado("text_response(finish_reason=stop)"))
        await run_queued(supervisor, task_id)
        tarea = {t.id: t for t in await _tareas(maker, agent_id)}[task_id]
        assert tarea.metadata_json["end_reason"] == "completed"
        assert "resultado_incompleto" not in tarea.metadata_json
        assert (await _avisos(maker, agent_id))[0].body.startswith("✅ Terminó")
        supervisor.submit_task.assert_not_awaited()

    async def test_segundo_plano_sin_salida_y_sin_acciones_se_continua_y_se_envia(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(
            maker, {"background_job": True, "thread_user_id": "@", "background_title": "Informe"}
        )
        supervisor = _supervisor(maker, error=RuntimeExecutionError(SIN_SALIDA))
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            await run_queued(supervisor, task_id)
        nuevas = [t for t in await _tareas(maker, agent_id) if (t.metadata_json or {}).get("origin_task_id")]
        assert len(nuevas) == 1
        assert "se quedó sin responder" in nuevas[0].prompt
        supervisor.submit_task.assert_awaited_once_with(nuevas[0].id)

    async def test_segundo_plano_sin_salida_con_accion_externa_no_se_repite(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(
            maker, {"background_job": True, "thread_user_id": "@", "background_title": "Informe"}
        )
        async with maker() as session:
            await session.execute(
                text(
                    "INSERT INTO external_action_records (id, task_id, attempt_id, lease_generation, kind, "
                    "fingerprint, state, created_at) VALUES (:id, :t, :a, 1, 'enviar_correo', :f, 'confirmada', now())"
                ),
                {"id": str(uuid.uuid4()), "t": task_id, "a": str(uuid.uuid4()), "f": uuid.uuid4().hex},
            )
            await session.commit()
        supervisor = _supervisor(maker, error=RuntimeExecutionError(SIN_SALIDA))
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            await run_queued(supervisor, task_id)
        assert [t.id for t in await _tareas(maker, agent_id) if t.id != task_id and t.prompt != "hola"] == []
        supervisor.submit_task.assert_not_awaited()
        avisos = await _avisos(maker, agent_id)
        assert len(avisos) == 1 and avisos[0].body.startswith("❌")
