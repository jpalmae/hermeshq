from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from conftest import requires_database
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers import agents_runtime, dashboard, logs, skills

pytestmark = [pytest.mark.integration, requires_database]


def _log(agent_id, event_type, message, task_id=None, severity="info", minutos=0):
    return ActivityLog(
        agent_id=agent_id,
        task_id=task_id,
        event_type=event_type,
        message=message,
        severity=severity,
        details={},
        created_at=datetime.now(UTC) - timedelta(minutes=minutos),
    )


@pytest_asyncio.fixture
async def mundo(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        nodo = Node(name="nodo-logs", hostname="localhost")
        ana = User(username="ana", display_name="Ana", password_hash="x")
        beto = User(username="beto", display_name="Beto", password_hash="x")
        carla = User(username="carla", display_name="Carla", password_hash="x")
        admin = User(username="admin-logs", display_name="Admin", password_hash="x", role="admin")
        db.add_all([nodo, ana, beto, carla, admin])
        await db.flush()
        agente = Agent(
            node_id=nodo.id, name="compartido", slug="compartido", workspace_path="/tmp/ws/compartido", status="running"
        )
        db.add(agente)
        await db.flush()
        db.add_all(
            [AgentAssignment(agent_id=agente.id, user_id=ana.id), AgentAssignment(agent_id=agente.id, user_id=beto.id)]
        )
        tarea_ana = Task(agent_id=agente.id, prompt="nomina de Ana", status="completed", created_by_user_id=ana.id)
        tarea_beto = Task(
            agent_id=agente.id,
            prompt="informe de Beto",
            status="running",
            metadata_json={"source": "public_chat", "thread_user_id": beto.id},
        )
        db.add_all([tarea_ana, tarea_beto])
        await db.flush()
        db.add_all(
            [
                _log(agente.id, "agent.output", "fragmento privado de Ana", tarea_ana.id, minutos=9),
                _log(agente.id, "task.completed", "inicio prompt privado de Ana", tarea_ana.id, minutos=8),
                _log(agente.id, "task.failed", "error privado de Ana", tarea_ana.id, severity="error", minutos=7),
                _log(agente.id, "agent.output", "fragmento de Beto", tarea_beto.id, minutos=6),
                _log(agente.id, "terminal.output", "terminal secreta sin tarea", minutos=5),
                _log(agente.id, "terminal.input", "comando secreto sin tarea", minutos=4),
                _log(agente.id, "agent.output", "salida huerfana sin tarea", minutos=3),
                _log(agente.id, "agent.started", "compartido started", minutos=2),
            ]
        )
        await db.commit()
        usuarios = {"ana": ana, "beto": beto, "carla": carla, "admin": admin}

    actual = {"usuario": admin}
    supervisor = SimpleNamespace(start_agent=AsyncMock(), stop_agent=AsyncMock(), restart_agent=AsyncMock())
    instalacion = SimpleNamespace(delete_installed_skill=AsyncMock(return_value=[]))
    app = FastAPI()
    for modulo in (logs, dashboard, agents_runtime, skills):
        app.include_router(modulo.router)
    app.state.supervisor = supervisor
    app.state.installation_manager = instalacion

    async def _db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_current_user] = lambda: actual["usuario"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hq")

    def como(nombre):
        actual["usuario"] = usuarios[nombre]
        return client

    try:
        yield SimpleNamespace(
            como=como,
            agente=agente.id,
            tarea_ana=tarea_ana.id,
            tarea_beto=tarea_beto.id,
            supervisor=supervisor,
            instalacion=instalacion,
        )
    finally:
        await client.aclose()


def _mensajes(items):
    return {item["message"] for item in items}


TEXTOS_DE_ANA = {"fragmento privado de Ana", "inicio prompt privado de Ana", "error privado de Ana"}
SENSIBLES_SIN_TAREA = {"terminal secreta sin tarea", "comando secreto sin tarea", "salida huerfana sin tarea"}


class TestLogsAislados:
    @pytest.mark.asyncio
    async def test_beto_por_agente_no_ve_tareas_de_ana_ni_terminal(self, mundo):
        r = await mundo.como("beto").get("/logs", params={"agent_id": mundo.agente})
        assert r.status_code == 200
        vistos = _mensajes(r.json()["items"])
        assert not vistos & TEXTOS_DE_ANA
        assert not vistos & SENSIBLES_SIN_TAREA
        assert {"fragmento de Beto", "compartido started"} <= vistos

    @pytest.mark.asyncio
    async def test_beto_por_tarea_de_ana_no_recibe_nada(self, mundo):
        r = await mundo.como("beto").get("/logs", params={"task_id": mundo.tarea_ana})
        assert r.status_code == 200
        assert r.json()["items"] == []

    @pytest.mark.asyncio
    async def test_beto_busqueda_textual_no_filtra_contenido_ajeno(self, mundo):
        r = await mundo.como("beto").get("/logs", params={"query": "privado"})
        assert r.json()["items"] == []

    @pytest.mark.asyncio
    async def test_ana_sin_filtro_ve_lo_suyo_y_no_lo_de_beto(self, mundo):
        vistos = _mensajes((await mundo.como("ana").get("/logs")).json()["items"])
        assert vistos >= TEXTOS_DE_ANA
        assert "fragmento de Beto" not in vistos
        assert not vistos & SENSIBLES_SIN_TAREA

    @pytest.mark.asyncio
    async def test_admin_ve_todo(self, mundo):
        vistos = _mensajes((await mundo.como("admin").get("/logs", params={"agent_id": mundo.agente})).json()["items"])
        assert TEXTOS_DE_ANA | SENSIBLES_SIN_TAREA | {"fragmento de Beto", "compartido started"} <= vistos


class TestDashboardAislado:
    @pytest.mark.asyncio
    async def test_actividad_de_beto_sin_contenido_de_ana(self, mundo):
        r = await mundo.como("beto").get("/dashboard/activity")
        assert r.status_code == 200
        vistos = _mensajes(r.json())
        assert not vistos & (TEXTOS_DE_ANA | SENSIBLES_SIN_TAREA)
        assert "fragmento de Beto" in vistos

    @pytest.mark.asyncio
    async def test_actividad_admin_completa(self, mundo):
        vistos = _mensajes((await mundo.como("admin").get("/dashboard/activity")).json())
        assert vistos >= TEXTOS_DE_ANA | SENSIBLES_SIN_TAREA

    @pytest.mark.asyncio
    async def test_overview_de_beto_sin_contenido_de_ana(self, mundo):
        vistos = _mensajes((await mundo.como("beto").get("/dashboard/overview")).json()["activity"])
        assert not vistos & (TEXTOS_DE_ANA | SENSIBLES_SIN_TAREA)

    @pytest.mark.asyncio
    async def test_errores_recientes_aislados(self, mundo):
        beto = [e["message"] for e in (await mundo.como("beto").get("/dashboard/health")).json()["recent_errors"]]
        carla = [e["message"] for e in (await mundo.como("carla").get("/dashboard/health")).json()["recent_errors"]]
        ana = [e["message"] for e in (await mundo.como("ana").get("/dashboard/health")).json()["recent_errors"]]
        admin = [e["message"] for e in (await mundo.como("admin").get("/dashboard/health")).json()["recent_errors"]]
        assert beto == [] and carla == []
        assert ana == ["error privado de Ana"] and admin == ["error privado de Ana"]

    @pytest.mark.asyncio
    async def test_estadisticas_de_tareas_por_visibilidad(self, mundo):
        beto = (await mundo.como("beto").get("/dashboard/tasks/stats")).json()
        ana = (await mundo.como("ana").get("/dashboard/tasks/stats")).json()
        admin = (await mundo.como("admin").get("/dashboard/tasks/stats")).json()
        assert beto == {"counts": {"running": 1}, "total": 1}
        assert ana == {"counts": {"completed": 1}, "total": 1}
        assert admin["total"] == 2


class TestContadoresAislados:
    @pytest.mark.asyncio
    async def test_overview_cuenta_solo_las_tareas_propias(self, mundo):
        beto = (await mundo.como("beto").get("/dashboard/overview")).json()["stats"]
        ana = (await mundo.como("ana").get("/dashboard/overview")).json()["stats"]
        admin = (await mundo.como("admin").get("/dashboard/overview")).json()["stats"]
        assert beto["total_tasks"] == 1 and ana["total_tasks"] == 1 and admin["total_tasks"] == 2
        assert (await mundo.como("carla").get("/dashboard/overview")).json()["stats"]["total_tasks"] == 0

    @pytest.mark.asyncio
    async def test_analytics_cuenta_solo_las_tareas_propias(self, mundo):
        def total(r):
            return sum(sum(dia.values()) for dia in r.json()["time_series"].values())

        assert total(await mundo.como("beto").get("/dashboard/analytics")) == 1
        assert total(await mundo.como("ana").get("/dashboard/analytics")) == 1
        assert total(await mundo.como("admin").get("/dashboard/analytics")) == 2
        assert total(await mundo.como("carla").get("/dashboard/analytics")) == 0


class TestOperacionesSoloAdmin:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("accion", ["stop", "restart"])
    async def test_asignado_no_detiene_ni_reinicia(self, mundo, accion):
        r = await mundo.como("beto").post(f"/agents/{mundo.agente}/{accion}")
        assert r.status_code == 403
        mundo.supervisor.stop_agent.assert_not_awaited()
        mundo.supervisor.restart_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_asignado_no_cambia_modo(self, mundo):
        r = await mundo.como("ana").post(f"/agents/{mundo.agente}/mode", json={"mode": "headless"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_asignado_no_borra_skill_instalada(self, mundo):
        r = await mundo.como("beto").delete(f"/skills/agents/{mundo.agente}/installed", params={"path": "x/SKILL.md"})
        assert r.status_code == 403
        mundo.instalacion.delete_installed_skill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_asignado_puede_arrancar_agente_detenido(self, mundo):
        r = await mundo.como("beto").post(f"/agents/{mundo.agente}/start")
        assert r.status_code == 200
        mundo.supervisor.start_agent.assert_awaited_once_with(mundo.agente)

    @pytest.mark.asyncio
    async def test_admin_conserva_operaciones(self, mundo):
        cliente = mundo.como("admin")
        assert (await cliente.post(f"/agents/{mundo.agente}/stop")).status_code == 200
        assert (await cliente.post(f"/agents/{mundo.agente}/restart")).status_code == 200
        assert (await cliente.post(f"/agents/{mundo.agente}/mode", json={"mode": "headless"})).status_code == 200
        r = await cliente.delete(f"/skills/agents/{mundo.agente}/installed", params={"path": "x/SKILL.md"})
        assert r.status_code == 200
        mundo.supervisor.stop_agent.assert_awaited_once_with(mundo.agente)
        mundo.supervisor.restart_agent.assert_awaited_once_with(mundo.agente)
        mundo.instalacion.delete_installed_skill.assert_awaited_once()
