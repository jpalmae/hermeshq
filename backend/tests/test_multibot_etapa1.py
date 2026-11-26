from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from conftest import requires_database
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_crons_propietario import _escenario

from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.routers import aprobaciones, internal_agents, public_chat
from hermeshq.services import aprobacion_central as ac
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.delegation_approval import approve_delegation_identity
from hermeshq.services.public_chat_service import PublicChatService, _hash_key, _prompt_con_referencia
from hermeshq.services.schedule_notifications import (
    enqueue_handoff_notice,
    enqueue_progress_notice,
    enqueue_respuesta_tardia,
    enqueue_schedule_notification,
)

VIVA = timedelta(minutes=1)
CAIDA = timedelta(hours=13)


def _clave(e, usuario, etiqueta, sondeo=VIVA):
    return PublicChatApiKey(
        key_hash=f"hash-{etiqueta}",
        key_prefix="pk_",
        label=f"bridge-teams-{etiqueta}",
        agent_id=e["agente"].id,
        allowed_domains=["*"],
        bound_user_id=e[usuario].id,
        last_notification_poll_at=(datetime.now(UTC) - sondeo) if sondeo is not None else None,
    )


async def _mundo(db, sondeo_bot2=VIVA):
    e = await _escenario(db)
    for tarea in e["tareas"].values():
        tarea.status = "completed"
    e["k1"] = _clave(e, "ana", "ana-bot1")
    e["k2"] = _clave(e, "ana", "ana-bot2", sondeo_bot2)
    e["kb"] = _clave(e, "beto", "beto-bot1")
    db.add_all([e["k1"], e["k2"], e["kb"]])
    await db.flush()
    for nombre, clave in (("s1", e["k1"]), ("s2", e["k2"])):
        e[nombre] = PublicChatSession(
            api_key_id=clave.id,
            agent_id=e["agente"].id,
            session_token=_hash_key(f"token-{nombre}"),
            status="active",
            last_activity=datetime.now(UTC),
            ttl_minutes=10,
        )
        db.add(e[nombre])
    await db.commit()
    return e


async def _avisos(db, **filtros):
    consulta = select(PublicChatNotification)
    for campo, valor in filtros.items():
        consulta = consulta.where(getattr(PublicChatNotification, campo) == valor)
    return list((await db.execute(consulta)).scalars().all())


def _tarea_chat(e, sesion, clave=None, **extra):
    metadata = {
        "conversation": True,
        "source": "public_chat",
        "public_session_id": e[sesion].id,
        "thread_user_id": e["ana"].id,
        **extra,
    }
    if clave is not None:
        metadata["public_chat_api_key_id"] = e[clave].id
    return Task(
        agent_id=e["agente"].id,
        prompt="hola",
        status="running",
        execution_lane=uuid4().hex[:16],
        metadata_json=metadata,
    )


def _request_notificaciones(api_key):
    service = MagicMock()
    service.validate_api_key = AsyncMock(return_value=api_key)
    service.notification_limiter = SimpleNamespace(check=lambda _k: True)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(public_chat_service=service)), headers={})


@pytest.mark.integration
@requires_database
class TestR10NotificacionesDelUsuarioFijo:
    async def test_clave_reasignada_no_ve_avisos_del_usuario_anterior(self, db_session):
        e = await _mundo(db_session)
        for usuario in ("ana", "beto"):
            db_session.add(
                PublicChatNotification(
                    api_key_id=e["k1"].id,
                    agent_id=e["agente"].id,
                    recipient_user_id=e[usuario].id,
                    title=f"de {usuario}",
                    body="x",
                )
            )
        await db_session.commit()
        items = await public_chat.list_notifications(_request_notificaciones(e["k1"]), 20, "k", db_session)
        assert [i.title for i in items] == ["de ana"]

    async def test_clave_de_confianza_sin_usuario_fijo_ve_todo_lo_suyo(self, db_session):
        e = await _mundo(db_session)
        e["k1"].bound_user_id = None
        e["k1"].trust_sender_id = True
        db_session.add(
            PublicChatNotification(
                api_key_id=e["k1"].id, agent_id=e["agente"].id, recipient_user_id=e["beto"].id, title="t", body="x"
            )
        )
        await db_session.commit()
        items = await public_chat.list_notifications(_request_notificaciones(e["k1"]), 20, "k", db_session)
        assert [i.title for i in items] == ["t"]


@pytest.mark.integration
@requires_database
class TestF2DuplicadosDeCron:
    def _payload(self, tarea, nombre):
        return internal_agents.InternalScheduleCreate(
            name=nombre, cron_expression="0 9 * * 1-5", prompt="Haz algo", task_id=tarea.id
        )

    async def test_no_expone_ni_bloquea_por_crons_de_otro_usuario(self, db_session):
        e = await _escenario(db_session)
        beto = e["crons"]["beto_aprobado"]
        res = await internal_agents.create_own_schedule(
            self._payload(e["tareas"]["ana"], beto.name), e["agente"], db_session
        )
        creado = await db_session.get(ScheduledTask, res["schedule_id"])
        assert creado.owner_user_id == e["ana"].id and beto.id not in str(res)

    async def test_duplicado_propio_sigue_detectandose(self, db_session):
        e = await _escenario(db_session)
        propio = e["crons"]["ana_aprobado"]
        with pytest.raises(HTTPException) as exc:
            await internal_agents.create_own_schedule(
                self._payload(e["tareas"]["ana"], propio.name), e["agente"], db_session
            )
        assert exc.value.status_code == 409 and propio.id in exc.value.detail


@pytest.mark.integration
@requires_database
class TestEntregaPorLaClaveDeOrigen:
    async def _preparar(self, db, tarea):
        db.add(tarea)
        await db.commit()
        return tarea

    async def test_respuesta_tardia_progreso_y_traspaso_solo_por_la_clave_de_origen(self, db_session):
        e = await _mundo(db_session)
        tarea = await self._preparar(db_session, _tarea_chat(e, "s1", "k1", respuesta_tardia=True))
        assert await enqueue_respuesta_tardia(db_session, tarea, success=True, summary="listo") == 1
        assert await enqueue_progress_notice(db_session, tarea, "sigo") == 1
        assert await enqueue_handoff_notice(db_session, tarea, "informe") == 1
        await db_session.commit()
        avisos = await _avisos(db_session, task_id=tarea.id)
        assert len(avisos) == 3 and {a.api_key_id for a in avisos} == {e["k1"].id}

    async def test_tarea_antigua_usa_la_clave_de_su_sesion(self, db_session):
        e = await _mundo(db_session)
        tarea = await self._preparar(db_session, _tarea_chat(e, "s2", None, respuesta_tardia=True))
        assert await enqueue_respuesta_tardia(db_session, tarea, success=True, summary="listo") == 1
        await db_session.commit()
        assert [a.api_key_id for a in await _avisos(db_session, task_id=tarea.id)] == [e["k2"].id]

    async def test_clave_de_origen_caida_no_desvia_al_otro_bot(self, db_session):
        e = await _mundo(db_session, sondeo_bot2=CAIDA)
        tarea = await self._preparar(db_session, _tarea_chat(e, "s2", "k2", respuesta_tardia=True))
        assert await enqueue_respuesta_tardia(db_session, tarea, success=True, summary="listo") == 0

    async def test_sin_clave_ni_sesion_conocida_se_aplica_la_regla_anterior(self, db_session):
        e = await _mundo(db_session)
        tarea = _tarea_chat(e, "s1", None, respuesta_tardia=True)
        tarea.metadata_json = {**tarea.metadata_json, "public_session_id": "sesion-purgada"}
        await self._preparar(db_session, tarea)
        assert await enqueue_respuesta_tardia(db_session, tarea, success=True, summary="listo") == 2

    async def test_segundo_plano_va_a_la_clave_del_chat_aunque_no_haya_sondeado(self, db_session):
        e = await _mundo(db_session, sondeo_bot2=CAIDA)
        trabajo = Task(
            agent_id=e["agente"].id,
            prompt="informe",
            status="completed",
            metadata_json={
                "background_job": True,
                "background_title": "Informe",
                "source": "background_job",
                "thread_user_id": e["ana"].id,
                "public_chat_api_key_id": e["k2"].id,
            },
        )
        await self._preparar(db_session, trabajo)
        assert await enqueue_schedule_notification(db_session, trabajo, success=True, summary="ok") == 1
        await db_session.commit()
        assert [a.api_key_id for a in await _avisos(db_session, task_id=trabajo.id)] == [e["k2"].id]

    async def test_segundo_plano_hereda_la_clave_por_la_tarea_de_origen(self, db_session):
        e = await _mundo(db_session)
        origen = await self._preparar(db_session, _tarea_chat(e, "s2", "k2"))
        trabajo = Task(
            agent_id=e["agente"].id,
            prompt="informe",
            status="completed",
            metadata_json={"background_job": True, "thread_user_id": e["ana"].id, "origin_task_id": origen.id},
        )
        await self._preparar(db_session, trabajo)
        assert await enqueue_schedule_notification(db_session, trabajo, success=True, summary="ok") == 1
        await db_session.commit()
        assert [a.api_key_id for a in await _avisos(db_session, task_id=trabajo.id)] == [e["k2"].id]

    async def test_mensaje_del_chat_guarda_la_clave_en_la_metadata(self, db_session):
        e = await _mundo(db_session)
        e["agente"].status = "running"
        await db_session.commit()
        supervisor = SimpleNamespace(active_tasks={}, submit_task=AsyncMock())
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), supervisor, None)
        task_id, _ = await servicio.send_message(e["s2"].id, "token-s2", "hola", db_session)
        tarea = await db_session.get(Task, task_id)
        assert tarea.metadata_json["public_chat_api_key_id"] == e["k2"].id


@pytest.mark.integration
@requires_database
class TestF5CronSinPuenteVivo:
    def _corrida(self, e, cron):
        return Task(
            agent_id=e["agente"].id,
            prompt=cron.prompt,
            status="completed",
            created_by_user_id=e["ana"].id,
            metadata_json={"scheduled": True, "scheduled_task_id": cron.id, "thread_user_id": e["ana"].id},
        )

    async def _cron(self, db, e):
        cron = e["crons"]["ana_aprobado"]
        cron.notify_public_chat = True
        corrida = self._corrida(e, cron)
        db.add(corrida)
        await db.commit()
        return corrida

    async def test_cron_con_puente_caido_queda_encolado_en_la_clave_activa(self, db_session):
        e = await _escenario(db_session)
        clave = _clave(e, "ana", "solo", sondeo=CAIDA)
        db_session.add(clave)
        corrida = await self._cron(db_session, e)
        assert await enqueue_schedule_notification(db_session, corrida, success=True, summary="ok") == 1
        await db_session.commit()
        assert [a.api_key_id for a in await _avisos(db_session, task_id=corrida.id)] == [clave.id]

    async def test_con_una_clave_viva_el_resultado_es_el_de_siempre(self, db_session):
        e = await _escenario(db_session)
        clave = _clave(e, "ana", "solo")
        db_session.add(clave)
        corrida = await self._cron(db_session, e)
        assert await enqueue_schedule_notification(db_session, corrida, success=True, summary="ok") == 1

    async def test_varias_vivas_sin_preferencia_difunde_y_avisa(self, db_session):
        from hermeshq.services import schedule_notifications

        e = await _mundo(db_session)
        corrida = await self._cron(db_session, e)
        with patch.object(schedule_notifications.logger, "warning") as aviso:
            assert await enqueue_schedule_notification(db_session, corrida, success=True, summary="ok") == 2
        assert any("notif_multiclave_sin_preferencia" in llamada.args[0] for llamada in aviso.call_args_list)

    async def test_solo_la_viva_si_la_otra_esta_caida(self, db_session):
        e = await _mundo(db_session, sondeo_bot2=CAIDA)
        corrida = await self._cron(db_session, e)
        assert await enqueue_schedule_notification(db_session, corrida, success=True, summary="ok") == 1
        await db_session.commit()
        assert [a.api_key_id for a in await _avisos(db_session, task_id=corrida.id)] == [e["k1"].id]


@pytest.mark.integration
@requires_database
class TestF6TareasDelChatPorClave:
    async def test_cancelar_en_el_bot2_no_toca_la_tarea_del_bot1(self, db_session):
        e = await _mundo(db_session)
        ajena = _tarea_chat(e, "s1", "k1")
        db_session.add(ajena)
        await db_session.commit()
        supervisor = SimpleNamespace(active_tasks={}, cancel_task=AsyncMock())
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), supervisor, None)
        respuesta = await servicio.handle_cancel_request(e["s2"].id, "token-s2", "cancela", db_session)
        await db_session.refresh(ajena)
        assert ajena.status == "running" and "No tengo ninguna tarea" in respuesta

    async def test_misma_clave_sigue_cancelando(self, db_session):
        e = await _mundo(db_session)
        propia = _tarea_chat(e, "s1", "k1")
        propia.metadata_json = {**propia.metadata_json, "public_session_id": "sesion-anterior"}
        db_session.add(propia)
        await db_session.commit()
        supervisor = SimpleNamespace(active_tasks={}, cancel_task=AsyncMock())
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), supervisor, None)
        await servicio.handle_cancel_request(e["s1"].id, "token-s1", "cancela", db_session)
        await db_session.refresh(propia)
        assert propia.status == "cancelled"

    def test_tarea_sin_clave_registrada_conserva_la_regla_anterior(self):
        tarea = SimpleNamespace(metadata_json={"source": "public_chat", "thread_user_id": "u1"})
        assert PublicChatService._task_belongs_to_chat(tarea, "s9", "u1", "k-otra") is True
        con_clave = SimpleNamespace(
            metadata_json={"source": "public_chat", "thread_user_id": "u1", "public_chat_api_key_id": "k1"}
        )
        assert PublicChatService._task_belongs_to_chat(con_clave, "s9", "u1", "k2") is False
        assert PublicChatService._task_belongs_to_chat(con_clave, "s9", "u1", "k1") is True


class _Resultado:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


def _previa(orden, usuario, response="Respuesta anterior", **extra):
    return SimpleNamespace(
        id=f"t{orden}",
        status="completed",
        response=response,
        queued_at=datetime.now(UTC) - timedelta(minutes=orden),
        metadata_json={"source": "public_chat", "public_session_id": "s1", "thread_user_id": usuario, **extra},
    )


class TestF3ReferenciaConResetAjeno:
    async def test_reset_de_otro_usuario_no_anula_la_referencia(self):
        tareas = [_previa(1, "u2", reset=True, public_session_id="s-otro"), _previa(2, "u1", "Te propongo el asunto X")]
        db = MagicMock()
        db.execute = AsyncMock(return_value=_Resultado(tareas))
        texto = await _prompt_con_referencia(db, "a1", "u1", "sí", "s1")
        assert "Te propongo el asunto X" in texto

    async def test_reset_propio_sigue_cortando(self):
        tareas = [_previa(1, "u1", reset=True), _previa(2, "u1", "Vieja")]
        db = MagicMock()
        db.execute = AsyncMock(return_value=_Resultado(tareas))
        assert await _prompt_con_referencia(db, "a1", "u1", "sí", "s1") == "sí"


def _ajustes():
    return SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)


@pytest.mark.integration
@requires_database
class TestF4ResumenDeCronPorClave:
    async def _entregado(self, db, e, clave, minutos=30):
        cron = e["crons"]["ana_aprobado"]
        entregado = datetime.now(UTC) - timedelta(minutes=minutos)
        corrida = Task(
            agent_id=e["agente"].id,
            title=cron.name,
            prompt=cron.prompt,
            status="completed",
            response="Resumen de Ana entregado",
            created_by_user_id=e["ana"].id,
            queued_at=entregado - timedelta(minutes=2),
            metadata_json={"scheduled": True, "scheduled_task_id": cron.id, "thread_user_id": e["ana"].id},
        )
        db.add(corrida)
        await db.flush()
        db.add(
            PublicChatNotification(
                api_key_id=e[clave].id,
                agent_id=e["agente"].id,
                recipient_user_id=e["ana"].id,
                schedule_id=cron.id,
                task_id=corrida.id,
                title=cron.name,
                body=corrida.response,
                status="delivered",
                created_at=entregado,
                delivered_at=entregado,
            )
        )
        await db.commit()
        return corrida

    async def _historial(self, db, tarea):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=_ajustes()):
            return await supervisor._build_conversation_history(db, tarea)

    def _texto(self, historial):
        return "\n".join(m["content"] for m in historial)

    async def test_el_resumen_del_bot2_no_entra_al_historial_del_bot1(self, db_session):
        e = await _mundo(db_session)
        await self._entregado(db_session, e, "k2")
        en_bot1 = _tarea_chat(e, "s1", "k1")
        en_bot2 = _tarea_chat(e, "s2", "k2")
        db_session.add_all([en_bot1, en_bot2])
        await db_session.commit()
        assert "Resumen de Ana entregado" not in self._texto(await self._historial(db_session, en_bot1))
        assert "Resumen de Ana entregado" in self._texto(await self._historial(db_session, en_bot2))

    async def test_reset_en_el_bot2_no_quita_el_resumen_del_bot1(self, db_session):
        e = await _mundo(db_session)
        await self._entregado(db_session, e, "k1", minutos=30)
        reset = _tarea_chat(e, "s2", "k2", reset=True)
        reset.status = "completed"
        reset.queued_at = datetime.now(UTC) - timedelta(minutes=5)
        en_bot1 = _tarea_chat(e, "s1", "k1")
        db_session.add_all([reset, en_bot1])
        await db_session.commit()
        assert "Resumen de Ana entregado" in self._texto(await self._historial(db_session, en_bot1))

    async def test_sin_clave_en_la_metadata_se_comporta_como_antes(self, db_session):
        e = await _mundo(db_session)
        await self._entregado(db_session, e, "k2")
        antigua = _tarea_chat(e, "s1", None)
        db_session.add(antigua)
        await db_session.commit()
        assert "Resumen de Ana entregado" in self._texto(await self._historial(db_session, antigua))


@pytest.mark.integration
@requires_database
class TestF9CodigoDeDelegacionAtomico:
    async def test_dos_peticiones_simultaneas_crean_una_sola_reejecucion(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as db:
            e = await _escenario(db)
            fallida = Task(
                agent_id=e["agente"].id,
                source_agent_id=e["agente"].id,
                prompt="dame tu calendario",
                status="failed",
                metadata_json={
                    "delegated": True,
                    "delegation_owner_user_id": e["ana"].id,
                    "identity_approval_code": "ABC123",
                    "identity_approval_expires": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            db.add(fallida)
            await db.commit()
            agente_id, ana_id = e["agente"].id, e["ana"].id
        async with maker() as primera, maker() as segunda:
            uno = await approve_delegation_identity(primera, agente_id, ana_id, "abc123")
            pendiente = asyncio.create_task(approve_delegation_identity(segunda, agente_id, ana_id, "ABC123"))
            await asyncio.sleep(0.5)
            await primera.commit()
            dos = await pendiente
            await segunda.commit()
        assert uno[0] is not None and dos == (None, None)
        async with maker() as db:
            reejecuciones = await db.execute(
                select(Task).where(Task.metadata_json["identity_granted"].as_string() == "true")
            )
            assert len(reejecuciones.scalars().all()) == 1


def _db_sin_identidades():
    db = MagicMock()
    vacio = MagicMock()
    vacio.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=vacio)
    db.get = AsyncMock(return_value=None)
    return db


class TestR9F8ErroresEstructurados:
    def _cuerpo(self, aad="aad-dueno"):
        return aprobaciones.DecisionBridge(
            session_id="s1", session_token="t", decision="aprobar", huella="h" * 64, aad_object_id=aad
        )

    async def test_sesion_caducada_tiene_codigo(self):
        servicio = MagicMock()
        servicio.validate_api_key = AsyncMock(return_value=SimpleNamespace(id="k1"))
        servicio.validate_session = AsyncMock(side_effect=ValueError("Session expired"))
        with pytest.raises(HTTPException) as exc:
            await aprobaciones.api_decision_bridge(servicio, MagicMock(), "k", "ap", self._cuerpo())
        assert exc.value.status_code == 401
        assert exc.value.detail["code"] == "sesion_caducada" and exc.value.detail["codigo"] == "sesion_caducada"

    async def test_sesion_de_otra_clave_es_otra_conversacion(self):
        servicio = MagicMock()
        servicio.validate_api_key = AsyncMock(return_value=SimpleNamespace(id="k2"))
        servicio.validate_session = AsyncMock(return_value=SimpleNamespace(id="s1", api_key_id="k1"))
        with pytest.raises(HTTPException) as exc:
            await aprobaciones.api_decision_bridge(servicio, MagicMock(), "k", "ap", self._cuerpo())
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "otra_conversacion"

    def test_estados_ya_decididos_y_expirada(self):
        for codigo, esperado, http in (
            ("rechazada", "ya_decidida", 409),
            ("estado_invalido", "ya_decidida", 409),
            ("expirada", "expirada", 410),
            ("otra_conversacion", "otra_conversacion", 409),
            ("otro_usuario", "otro_usuario", 404),
        ):
            exc = aprobaciones._http_bridge(ac.ErrorAprobacion(codigo))
            assert (exc.status_code, exc.detail["code"], exc.detail["codigo"]) == (http, esperado, codigo)

    async def test_clave_fija_sin_identidad_registrada_acepta(self):
        clave = SimpleNamespace(id="k1", trust_sender_id=False, bound_user_id="u1", bot_channel_id=None)
        sesion = SimpleNamespace(id="s1", sender_id=None, aad_object_id=None)
        assert (
            await aprobaciones.verificar_quien_pulsa(_db_sin_identidades(), clave, sesion, "u1", "aad-x", None) is None
        )

    async def test_clave_de_confianza_compara_con_el_remitente_de_la_sesion(self):
        clave = SimpleNamespace(id="k1", trust_sender_id=True, bound_user_id=None, bot_channel_id=None)
        sesion = SimpleNamespace(id="s1", sender_id="aad-dueno", aad_object_id=None)
        db = _db_sin_identidades()
        assert await aprobaciones.verificar_quien_pulsa(db, clave, sesion, "u1", "aad-dueno", None) is None
        assert await aprobaciones.verificar_quien_pulsa(db, clave, sesion, "u1", "aad-otro", None)
