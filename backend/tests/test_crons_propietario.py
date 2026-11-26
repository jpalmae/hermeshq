from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from conftest import requires_database
from fastapi import HTTPException
from sqlalchemy import select

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers import internal_agents, scheduled_tasks
from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, codigo_eliminacion
from hermeshq.services.public_chat_service import _prompt_con_referencia

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
)
CODIGO = "K7M2QX"


def _plugin():
    spec = importlib.util.spec_from_file_location("hermeshq_schedule_plugin_propietario", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cron(agent_id, owner_id, nombre, estado, codigo=None):
    return ScheduledTask(
        agent_id=agent_id,
        name=nombre,
        cron_expression="0 8 * * 1-5",
        prompt=f"Prompt privado de {nombre}",
        enabled=estado == "approved",
        timezone=DEFAULT_TIMEZONE,
        owner_user_id=owner_id,
        approval_status=estado,
        approval_code=codigo,
        created_by_agent_id=agent_id,
    )


def _tarea(agent_id, user_id, prompt="hola", status="running", **metadata):
    return Task(
        agent_id=agent_id,
        prompt=prompt,
        status=status,
        execution_lane=uuid4().hex[:16],
        metadata_json={"source": "public_chat", "thread_user_id": user_id, **metadata},
    )


async def _escenario(db):
    nodo = Node(name="nodo-crons", hostname="localhost")
    ana = User(username="ana", display_name="Ana", password_hash="x")
    beto = User(username="beto", display_name="Beto", password_hash="x")
    admin = User(username="admin-crons", display_name="Admin", password_hash="x", role="admin")
    db.add_all([nodo, ana, beto, admin])
    await db.flush()
    agente = Agent(node_id=nodo.id, name="compartido", slug="compartido", workspace_path="/tmp/ws/compartido")
    db.add(agente)
    await db.flush()
    db.add_all(
        [AgentAssignment(agent_id=agente.id, user_id=ana.id), AgentAssignment(agent_id=agente.id, user_id=beto.id)]
    )
    crons = {
        "ana_pendiente": _cron(agente.id, ana.id, "Informe de Ana", "pending", CODIGO),
        "ana_aprobado": _cron(agente.id, ana.id, "Resumen de Ana", "approved"),
        "beto_aprobado": _cron(agente.id, beto.id, "Nomina de Beto", "approved"),
    }
    db.add_all(crons.values())
    tareas = {"ana": _tarea(agente.id, ana.id), "beto": _tarea(agente.id, beto.id)}
    db.add_all(tareas.values())
    await db.commit()
    return {"agente": agente, "ana": ana, "beto": beto, "admin": admin, "crons": crons, "tareas": tareas}


async def _nueva_tarea(db, e, usuario, prompt, **metadata):
    tarea = _tarea(e["agente"].id, e[usuario].id, prompt, **metadata)
    db.add(tarea)
    await db.commit()
    return tarea


@pytest.mark.integration
@requires_database
class TestListadoInterno:
    async def test_cada_usuario_ve_solo_sus_crons(self, db_session):
        e = await _escenario(db_session)
        res = await internal_agents.list_own_schedules(e["agente"], db_session, task_id=e["tareas"]["ana"].id)
        assert {s["name"] for s in res["schedules"]} == {"Informe de Ana", "Resumen de Ana"}
        assert "Nomina de Beto" not in json.dumps(res)
        res = await internal_agents.list_own_schedules(e["agente"], db_session, task_id=e["tareas"]["beto"].id)
        assert [s["name"] for s in res["schedules"]] == ["Nomina de Beto"]

    async def test_el_pendiente_trae_su_codigo_y_solo_para_su_dueno(self, db_session):
        e = await _escenario(db_session)
        res = await internal_agents.list_own_schedules(e["agente"], db_session, task_id=e["tareas"]["ana"].id)
        por_nombre = {s["name"]: s for s in res["schedules"]}
        assert por_nombre["Informe de Ana"]["approval_code"] == CODIGO
        assert "approval_code" not in por_nombre["Resumen de Ana"]
        res = await internal_agents.list_own_schedules(e["agente"], db_session, task_id=e["tareas"]["beto"].id)
        assert CODIGO not in json.dumps(res)

    async def test_sin_tarea_viva_no_lista(self, db_session):
        e = await _escenario(db_session)
        terminada = await _nueva_tarea(db_session, e, "ana", "x")
        terminada.status = "completed"
        await db_session.commit()
        for task_id in (None, "no-existe", terminada.id):
            with pytest.raises(HTTPException) as exc:
                await internal_agents.list_own_schedules(e["agente"], db_session, task_id=task_id)
            assert exc.value.status_code == 403


@pytest.mark.integration
@requires_database
class TestCreacionInterna:
    def _payload(self, e, task_id, requested):
        return internal_agents.InternalScheduleCreate(
            name="Nuevo",
            cron_expression="0 9 * * 1-5",
            prompt="Haz algo nuevo",
            requested_for_user_id=requested,
            task_id=task_id,
        )

    async def test_no_crea_a_nombre_de_otro_usuario(self, db_session):
        e = await _escenario(db_session)
        payload = self._payload(e, e["tareas"]["ana"].id, e["beto"].id)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.create_own_schedule(payload, e["agente"], db_session)
        assert exc.value.status_code == 403
        filas = await db_session.execute(select(ScheduledTask).where(ScheduledTask.name == "Nuevo"))
        assert filas.scalars().all() == []

    async def test_sin_tarea_no_crea(self, db_session):
        e = await _escenario(db_session)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.create_own_schedule(self._payload(e, None, e["ana"].id), e["agente"], db_session)
        assert exc.value.status_code == 403

    async def test_crea_para_el_usuario_de_la_tarea(self, db_session):
        e = await _escenario(db_session)
        res = await internal_agents.create_own_schedule(
            self._payload(e, e["tareas"]["ana"].id, e["ana"].id), e["agente"], db_session
        )
        creado = await db_session.get(ScheduledTask, res["schedule_id"])
        assert creado.owner_user_id == e["ana"].id
        assert creado.notify_public_chat is True


@pytest.mark.integration
@requires_database
class TestBorradoInterno:
    async def _borrar(self, db, e, clave_cron, tarea):
        return await internal_agents.delete_own_schedule(e["crons"][clave_cron].id, e["agente"], db, task_id=tarea.id)

    async def test_no_borra_el_cron_de_otro_usuario(self, db_session):
        e = await _escenario(db_session)
        codigo = codigo_eliminacion(e["crons"]["beto_aprobado"])
        tarea = await _nueva_tarea(db_session, e, "ana", f"eliminar {codigo}")
        with pytest.raises(HTTPException) as exc:
            await self._borrar(db_session, e, "beto_aprobado", tarea)
        assert exc.value.status_code == 404
        assert await db_session.get(ScheduledTask, e["crons"]["beto_aprobado"].id) is not None

    async def test_sin_tarea_no_borra(self, db_session):
        e = await _escenario(db_session)
        with pytest.raises(HTTPException) as exc:
            await internal_agents.delete_own_schedule(
                e["crons"]["ana_pendiente"].id, e["agente"], db_session, task_id=None
            )
        assert exc.value.status_code == 403

    async def test_aprobado_pide_confirmacion_y_luego_se_borra(self, db_session):
        e = await _escenario(db_session)
        cron_id = e["crons"]["ana_aprobado"].id
        with pytest.raises(HTTPException) as exc:
            await self._borrar(db_session, e, "ana_aprobado", e["tareas"]["ana"])
        assert exc.value.status_code == 409
        codigo = codigo_eliminacion(e["crons"]["ana_aprobado"])
        assert f"eliminar {codigo}" in exc.value.detail
        assert await db_session.get(ScheduledTask, cron_id) is not None
        confirmacion = await _nueva_tarea(db_session, e, "ana", f"eliminar {codigo}")
        res = await self._borrar(db_session, e, "ana_aprobado", confirmacion)
        assert res["deleted"] == cron_id
        db_session.expunge_all()
        assert await db_session.get(ScheduledTask, cron_id) is None

    async def test_el_codigo_de_borrado_en_la_cita_del_agente_no_confirma(self, db_session):
        e = await _escenario(db_session)
        codigo = codigo_eliminacion(e["crons"]["ana_aprobado"])
        prompt = f"[Respuesta corta a tu último mensaje: «Responde eliminar {codigo}». Interpreta «ok» como respuesta a eso, no a temas anteriores.]\nok"
        tarea = await _nueva_tarea(db_session, e, "ana", prompt)
        with pytest.raises(HTTPException) as exc:
            await self._borrar(db_session, e, "ana_aprobado", tarea)
        assert exc.value.status_code == 409

    async def test_pendiente_propio_se_borra_sin_confirmacion(self, db_session):
        e = await _escenario(db_session)
        res = await self._borrar(db_session, e, "ana_pendiente", e["tareas"]["ana"])
        assert res["success"] is True


@pytest.mark.integration
@requires_database
class TestAprobacionConCita:
    async def _prompt_con_cita(self, db, e):
        previa = _tarea(e["agente"].id, e["ana"].id, "programa el informe", status="completed")
        previa.response = f"Listo. Para activarlo responde exactamente: aprobar {CODIGO}"
        previa.queued_at = datetime.now(UTC)
        db.add(previa)
        await db.commit()
        prompt = await _prompt_con_referencia(db, e["agente"].id, e["ana"].id, "ok")
        assert CODIGO in prompt
        return prompt

    async def _aprobar(self, db, e, tarea):
        cuerpo = internal_agents.InternalScheduleApprove(task_id=tarea.id)
        return await internal_agents.approve_own_schedule(e["crons"]["ana_pendiente"].id, cuerpo, e["agente"], db)

    async def test_cita_del_agente_no_aprueba_sin_metadata(self, db_session):
        e = await _escenario(db_session)
        tarea = await _nueva_tarea(db_session, e, "ana", await self._prompt_con_cita(db_session, e))
        with pytest.raises(HTTPException) as exc:
            await self._aprobar(db_session, e, tarea)
        assert exc.value.status_code == 403
        assert e["crons"]["ana_pendiente"].approval_status == "pending"

    async def test_cita_del_agente_no_aprueba_con_metadata(self, db_session):
        e = await _escenario(db_session)
        prompt = await self._prompt_con_cita(db_session, e)
        tarea = await _nueva_tarea(db_session, e, "ana", prompt, mensaje_usuario="ok")
        with pytest.raises(HTTPException) as exc:
            await self._aprobar(db_session, e, tarea)
        assert exc.value.status_code == 403

    async def test_trabajo_en_segundo_plano_no_aprueba(self, db_session):
        e = await _escenario(db_session)
        tarea = await _nueva_tarea(db_session, e, "ana", f"aprobar {CODIGO}", background_job=True)
        with pytest.raises(HTTPException) as exc:
            await self._aprobar(db_session, e, tarea)
        assert exc.value.status_code == 403

    async def test_mensaje_del_usuario_con_codigo_aprueba(self, db_session):
        e = await _escenario(db_session)
        tarea = await _nueva_tarea(db_session, e, "ana", f"aprobar {CODIGO}", mensaje_usuario=f"aprobar {CODIGO}")
        res = await self._aprobar(db_session, e, tarea)
        assert res["approval_status"] == "approved"


@pytest.mark.integration
@requires_database
class TestListadoWeb:
    async def test_asignado_ve_solo_los_suyos_y_admin_todos(self, db_session):
        e = await _escenario(db_session)
        de_ana = await scheduled_tasks.list_scheduled_tasks(e["ana"], db_session)
        assert {s.name for s in de_ana} == {"Informe de Ana", "Resumen de Ana"}
        de_beto = await scheduled_tasks.list_scheduled_tasks(e["beto"], db_session)
        assert [s.name for s in de_beto] == ["Nomina de Beto"]
        del_admin = await scheduled_tasks.list_scheduled_tasks(e["admin"], db_session)
        assert len(del_admin) == 3


class TestPluginEnviaTarea:
    def _llamar(self, funcion, args, payload):
        plugin = _plugin()
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_api_request", return_value="{}") as api,
        ):
            resultado = getattr(plugin, funcion)(args)
        return resultado, api

    def test_listar_envia_task_id(self):
        _, api = self._llamar("hq_schedule_list_tool", {}, {"task_id": "t-9", "metadata": {}})
        api.assert_called_once_with("GET", "/agents/self/schedules?task_id=t-9")

    def test_borrar_envia_task_id(self):
        _, api = self._llamar("hq_schedule_delete_tool", {"schedule_id": "s1"}, {"task_id": "t-9", "metadata": {}})
        api.assert_called_once_with("DELETE", "/agents/self/schedules/s1?task_id=t-9")

    def test_sin_tarea_no_llama_al_servidor(self):
        for funcion, args in (("hq_schedule_list_tool", {}), ("hq_schedule_delete_tool", {"schedule_id": "s1"})):
            resultado, api = self._llamar(funcion, args, {"metadata": {}})
            assert json.loads(resultado)["success"] is False
            api.assert_not_called()
