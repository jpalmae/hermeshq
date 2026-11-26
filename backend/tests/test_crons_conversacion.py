from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from conftest import requires_database
from fastapi import HTTPException
from sqlalchemy import select
from test_crons_propietario import _escenario, _nueva_tarea, _plugin

from hermeshq.models.activity import ActivityLog
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.routers import internal_agents, scheduled_tasks
from hermeshq.schemas.scheduled_task import ScheduledTaskUpdate
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.cron_schedule import codigo_eliminacion

RESUMEN = "| # | Remitente | Asunto |\n|---|---|---|\n| 1 | Proveedor Uno | Factura octubre |\n| 2 | Cliente Dos | Reclamo despacho |"


class _Supervisor:
    def __init__(self):
        self.enviadas: list[str] = []

    async def submit_task(self, task_id):
        self.enviadas.append(task_id)


def _request(supervisor):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor=supervisor)))


async def _eventos(db, tipo):
    filas = await db.execute(select(ActivityLog).where(ActivityLog.event_type == tipo))
    return list(filas.scalars().all())


async def _ejecuciones_del_cron(db, cron_id):
    filas = await db.execute(select(Task).where(Task.metadata_json["scheduled_task_id"].as_string() == cron_id))
    return list(filas.scalars().all())


def _accion(tarea):
    return internal_agents.InternalScheduleAction(task_id=tarea.id)


@pytest.mark.integration
@requires_database
class TestEjecutarAhora:
    async def test_el_dueno_ejecuta_el_cron_real(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        cron.output_format = "Tabla: Remitente | Asunto"
        await db_session.commit()
        supervisor = _Supervisor()
        res = await internal_agents.run_own_schedule(
            cron.id, _accion(e["tareas"]["ana"]), _request(supervisor), e["agente"], db_session
        )
        corrida = await db_session.get(Task, res["run_task_id"])
        md = corrida.metadata_json
        assert md["scheduled_task_id"] == cron.id
        assert md["scheduled"] is True and md["manual_trigger"] is True
        assert md["thread_user_id"] == e["ana"].id and corrida.created_by_user_id == e["ana"].id
        assert "Tabla: Remitente | Asunto" in corrida.prompt
        assert supervisor.enviadas == [corrida.id]
        [evento] = await _eventos(db_session, "schedule.triggered")
        assert evento.details["actor_user_id"] == e["ana"].id and evento.details["manual"] is True

    async def test_otro_usuario_no_puede_ejecutarlo(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        supervisor = _Supervisor()
        with pytest.raises(HTTPException) as exc:
            await internal_agents.run_own_schedule(
                cron.id, _accion(e["tareas"]["beto"]), _request(supervisor), e["agente"], db_session
            )
        assert exc.value.status_code == 404
        assert await _ejecuciones_del_cron(db_session, cron.id) == []
        assert supervisor.enviadas == []

    async def test_no_se_ejecuta_dos_veces_a_la_vez(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        supervisor = _Supervisor()
        await internal_agents.run_own_schedule(
            cron.id, _accion(e["tareas"]["ana"]), _request(supervisor), e["agente"], db_session
        )
        with pytest.raises(HTTPException) as exc:
            await internal_agents.run_own_schedule(
                cron.id, _accion(e["tareas"]["ana"]), _request(supervisor), e["agente"], db_session
            )
        assert exc.value.status_code == 409
        assert len(await _ejecuciones_del_cron(db_session, cron.id)) == 1

    async def test_pendiente_o_desde_una_corrida_programada_no_se_ejecuta(self, db_session):
        e = await _escenario(db_session)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.run_own_schedule(
                e["crons"]["ana_pendiente"].id,
                _accion(e["tareas"]["ana"]),
                _request(_Supervisor()),
                e["agente"],
                db_session,
            )
        assert exc.value.status_code == 409
        programada = await _nueva_tarea(db_session, e, "ana", "corre", scheduled=True)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.run_own_schedule(
                e["crons"]["ana_aprobado"].id, _accion(programada), _request(_Supervisor()), e["agente"], db_session
            )
        assert exc.value.status_code == 403


@pytest.mark.integration
@requires_database
class TestPausarReanudar:
    async def test_el_dueno_pausa_y_reanuda_sin_borrar(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        res = await internal_agents.pause_own_schedule(cron.id, _accion(e["tareas"]["ana"]), e["agente"], db_session)
        assert res["enabled"] is False and res["changed"] is True
        db_session.expunge_all()
        guardado = await db_session.get(ScheduledTask, cron.id)
        assert guardado is not None and guardado.enabled is False and guardado.approval_status == "approved"
        res = await internal_agents.resume_own_schedule(cron.id, _accion(e["tareas"]["ana"]), e["agente"], db_session)
        assert res["enabled"] is True and res["next_run_local"]
        [pausa] = await _eventos(db_session, "schedule.disabled")
        [reanuda] = await _eventos(db_session, "schedule.enabled")
        assert pausa.details["actor_user_id"] == reanuda.details["actor_user_id"] == e["ana"].id

    async def test_otro_usuario_no_puede_pausar(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        for funcion in (internal_agents.pause_own_schedule, internal_agents.resume_own_schedule):
            with pytest.raises(HTTPException) as exc:
                await funcion(cron.id, _accion(e["tareas"]["beto"]), e["agente"], db_session)
            assert exc.value.status_code == 404
        db_session.expunge_all()
        assert (await db_session.get(ScheduledTask, cron.id)).enabled is True
        assert await _eventos(db_session, "schedule.disabled") == []


@pytest.mark.integration
@requires_database
class TestEjecucionesDelCron:
    async def _corrida(self, db, e, cron, usuario, horas, status, response=None, error=None):
        tarea = Task(
            agent_id=e["agente"].id,
            prompt=cron.prompt,
            status=status,
            execution_lane=uuid4().hex[:16],
            response=response,
            error_message=error,
            created_by_user_id=e[usuario].id,
            queued_at=datetime.now(UTC) - timedelta(hours=horas),
            metadata_json={"scheduled": True, "scheduled_task_id": cron.id, "thread_user_id": e[usuario].id},
        )
        db.add(tarea)
        await db.commit()
        return tarea

    async def test_el_dueno_ve_sus_ultimas_ejecuciones_y_el_otro_no(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        await self._corrida(db_session, e, cron, "ana", 30, "completed", response="Resumen viejo " + "x" * 600)
        await self._corrida(db_session, e, cron, "ana", 5, "failed", error="Hermes runtime exceeded the task timeout")
        await self._corrida(db_session, e, cron, "ana", 1, "completed", response="Sin correos nuevos")
        await self._corrida(db_session, e, e["crons"]["beto_aprobado"], "beto", 1, "completed", response="Nomina")
        res = await internal_agents.list_own_schedule_runs(
            cron.id, e["agente"], db_session, task_id=e["tareas"]["ana"].id, limit=2
        )
        assert [r["estado"] for r in res["runs"]] == ["completed", "failed"]
        assert res["runs"][0]["resumen"] == "Sin correos nuevos" and res["runs"][0]["fecha_local"]
        todas = await internal_agents.list_own_schedule_runs(
            cron.id, e["agente"], db_session, task_id=e["tareas"]["ana"].id, limit=20
        )
        assert len(todas["runs"]) == 3 and len(todas["runs"][2]["resumen"]) <= internal_agents.RUN_SUMMARY_CHARS
        assert "Nomina" not in json.dumps(todas)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.list_own_schedule_runs(
                cron.id, e["agente"], db_session, task_id=e["tareas"]["beto"].id, limit=5
            )
        assert exc.value.status_code == 404


@pytest.mark.integration
@requires_database
class TestBitacoraDeCambios:
    async def test_web_put_y_delete_dejan_actor(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        await scheduled_tasks.update_scheduled_task(
            cron.id, ScheduledTaskUpdate(prompt="Nuevo prompt de Ana"), e["ana"], db_session
        )
        [actualizado] = await _eventos(db_session, "schedule.updated")
        assert actualizado.details["actor_user_id"] == e["ana"].id
        assert actualizado.details["fields"] == ["prompt"] and actualizado.details["requires_reapproval"] is True
        await scheduled_tasks.update_scheduled_task(
            e["crons"]["beto_aprobado"].id, ScheduledTaskUpdate(enabled=False), e["admin"], db_session
        )
        [pausa] = await _eventos(db_session, "schedule.disabled")
        assert pausa.details["actor_user_id"] == e["admin"].id
        await scheduled_tasks.delete_scheduled_task(cron.id, e["ana"], db_session)
        [borrado] = await _eventos(db_session, "schedule.deleted")
        assert borrado.details == {
            "schedule_id": cron.id,
            "actor_user_id": e["ana"].id,
            "channel": "web",
            "owner_user_id": e["ana"].id,
            "approval_status": "pending",
        }

    async def test_web_sin_cambios_no_registra_y_ajeno_no_registra(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        await scheduled_tasks.update_scheduled_task(cron.id, ScheduledTaskUpdate(name=cron.name), e["ana"], db_session)
        with pytest.raises(HTTPException):
            await scheduled_tasks.delete_scheduled_task(cron.id, e["beto"], db_session)
        assert await _eventos(db_session, "schedule.updated") == []
        assert await _eventos(db_session, "schedule.deleted") == []

    async def test_borrado_por_chat_deja_actor(self, db_session):
        e = await _escenario(db_session)
        cron = e["crons"]["ana_aprobado"]
        tarea = await _nueva_tarea(db_session, e, "ana", f"eliminar {codigo_eliminacion(cron)}")
        await internal_agents.delete_own_schedule(cron.id, e["agente"], db_session, task_id=tarea.id)
        [borrado] = await _eventos(db_session, "schedule.deleted_by_agent")
        assert borrado.details["actor_user_id"] == e["ana"].id and borrado.details["channel"] == "public_chat"


def _ajustes():
    return SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)


@pytest.mark.integration
@requires_database
class TestResumenProgramadoEnElHistorial:
    async def _resumen_entregado(self, db, e, usuario="ana", minutos=30, estado="delivered"):
        cron = e["crons"][f"{usuario}_aprobado"]
        clave = PublicChatApiKey(
            key_hash=f"hash-{usuario}-{minutos}-{estado}", key_prefix="hq_", label="teams", agent_id=e["agente"].id
        )
        db.add(clave)
        entregado = datetime.now(UTC) - timedelta(minutes=minutos)
        corrida = Task(
            agent_id=e["agente"].id,
            title=cron.name,
            prompt=cron.prompt,
            status="completed",
            response=RESUMEN if usuario == "ana" else "Nomina privada de Beto",
            created_by_user_id=e[usuario].id,
            queued_at=entregado - timedelta(minutes=2),
            metadata_json={"scheduled": True, "scheduled_task_id": cron.id, "thread_user_id": e[usuario].id},
        )
        db.add(corrida)
        await db.flush()
        db.add(
            PublicChatNotification(
                api_key_id=clave.id,
                agent_id=e["agente"].id,
                recipient_user_id=e[usuario].id,
                schedule_id=cron.id,
                task_id=corrida.id,
                title=cron.name,
                body=corrida.response,
                status=estado,
                created_at=entregado,
                delivered_at=entregado if estado == "delivered" else None,
            )
        )
        await db.commit()
        return corrida

    async def _turno(self, db, e, usuario, prompt, minutos=0, sesion="S-nueva", status="running", **extra):
        tarea = Task(
            agent_id=e["agente"].id,
            prompt=prompt,
            status=status,
            execution_lane=uuid4().hex[:16],
            response="respuesta previa" if status == "completed" else None,
            queued_at=datetime.now(UTC) - timedelta(minutes=minutos),
            metadata_json={
                "source": "public_chat",
                "conversation": True,
                "public_session_id": sesion,
                "thread_user_id": e[usuario].id,
                **extra,
            },
        )
        db.add(tarea)
        await db.commit()
        return tarea

    async def _historial(self, db, tarea):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=_ajustes()):
            return await supervisor._build_conversation_history(db, tarea)

    async def test_el_resumen_entregado_entra_al_historial_del_dueno(self, db_session):
        e = await _escenario(db_session)
        corrida = await self._resumen_entregado(db_session, e)
        actual = await self._turno(db_session, e, "ana", "el segundo, respóndele")
        historial = await self._historial(db_session, actual)
        assert len(historial) == 2
        assert historial[0]["role"] == "user"
        assert historial[0]["content"].startswith("[Resumen programado «Resumen de Ana»")
        assert f"task_id={corrida.id}" in historial[0]["content"]
        assert historial[1] == {"role": "assistant", "content": RESUMEN}

    async def test_nunca_entra_al_historial_de_otro_usuario(self, db_session):
        e = await _escenario(db_session)
        await self._resumen_entregado(db_session, e, "ana")
        actual = await self._turno(db_session, e, "beto", "el segundo")
        historial = await self._historial(db_session, actual)
        assert "Reclamo despacho" not in json.dumps(historial, ensure_ascii=False)
        assert historial == []

    async def test_un_reset_posterior_lo_deja_fuera(self, db_session):
        e = await _escenario(db_session)
        await self._resumen_entregado(db_session, e, minutos=30)
        await self._turno(db_session, e, "ana", "/reset", minutos=10, sesion="S-vieja", status="completed", reset=True)
        actual = await self._turno(db_session, e, "ana", "el segundo")
        assert await self._historial(db_session, actual) == []

    async def test_no_entregado_o_fuera_de_ventana_no_entra(self, db_session):
        e = await _escenario(db_session)
        await self._resumen_entregado(db_session, e, minutos=30, estado="pending")
        await self._resumen_entregado(db_session, e, minutos=13 * 60)
        actual = await self._turno(db_session, e, "ana", "el segundo")
        assert await self._historial(db_session, actual) == []

    async def test_se_ubica_en_orden_y_respeta_los_turnos_conservados(self, db_session):
        e = await _escenario(db_session)
        await self._turno(db_session, e, "ana", "pregunta antes", minutos=60, sesion="S1", status="completed")
        await self._resumen_entregado(db_session, e, minutos=30)
        await self._turno(db_session, e, "ana", "el segundo", minutos=5, sesion="S1", status="completed")
        actual = await self._turno(db_session, e, "ana", "respóndele", sesion="S1")
        historial = await self._historial(db_session, actual)
        contenidos = [m["content"] for m in historial]
        assert len(historial) == 6
        assert "pregunta antes" in contenidos[0]
        assert contenidos[2].startswith("[Resumen programado") and contenidos[3] == RESUMEN
        assert "el segundo" in contenidos[4]

    async def test_mas_antiguo_que_los_turnos_conservados_no_entra(self, db_session):
        e = await _escenario(db_session)
        await self._resumen_entregado(db_session, e, minutos=90)
        from hermeshq.services.agent_supervisor import HISTORY_MAX_TURNS

        for i in range(HISTORY_MAX_TURNS):
            await self._turno(db_session, e, "ana", f"turno {i}", minutos=80 - 10 * i, sesion="S1", status="completed")
        actual = await self._turno(db_session, e, "ana", "ultimo", sesion="S1")
        historial = await self._historial(db_session, actual)
        assert not any(m["content"].startswith("[Resumen programado") for m in historial)


class TestPluginAccionesSobreCrons:
    def _llamar(self, funcion, args, payload):
        plugin = _plugin()
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_api_request", return_value="{}") as api,
        ):
            resultado = getattr(plugin, funcion)(args)
        return resultado, api

    def test_ejecutar_pausar_y_reanudar_envian_la_tarea_viva(self):
        for funcion, accion in (
            ("hq_schedule_run_tool", "run"),
            ("hq_schedule_pause_tool", "pause"),
            ("hq_schedule_resume_tool", "resume"),
        ):
            _, api = self._llamar(funcion, {"schedule_id": "s1"}, {"task_id": "t-9", "metadata": {}})
            api.assert_called_once_with("POST", f"/agents/self/schedules/s1/{accion}", {"task_id": "t-9"})

    def test_ejecuciones_envian_task_id_y_limite_acotado(self):
        _, api = self._llamar("hq_schedule_runs_tool", {"schedule_id": "s1", "limit": 99}, {"task_id": "t-9"})
        api.assert_called_once_with("GET", "/agents/self/schedules/s1/runs?task_id=t-9&limit=20")

    def test_desde_una_corrida_programada_o_sin_tarea_no_llama(self):
        for payload in ({"task_id": "t-9", "metadata": {"scheduled": True}}, {"metadata": {}}):
            resultado, api = self._llamar("hq_schedule_run_tool", {"schedule_id": "s1"}, payload)
            assert json.loads(resultado)["success"] is False
            api.assert_not_called()

    def test_las_herramientas_quedan_registradas(self):
        plugin = _plugin()
        nombres = []
        plugin.register(SimpleNamespace(register_tool=lambda **kw: nombres.append(kw["name"])))
        assert {"hq_schedule_run", "hq_schedule_pause", "hq_schedule_resume", "hq_schedule_runs"} <= set(nombres)
