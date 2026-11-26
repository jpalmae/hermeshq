from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import requires_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services import auto_background
from hermeshq.services import referencias_correo as rc
from hermeshq.services.archivos_conversacion import ENCABEZADO, linea_archivos
from hermeshq.services.formato_pedido import DIRECTIVA_EXCEL, DIRECTIVA_HTML
from hermeshq.services.public_chat_service import AVISO_PEDIDO_LARGO, PublicChatService, _hash_key

PEDIDO_TURNO_2 = (
    "usando la skill de informes interactivos, hazme un informe interactivo con las ventas de enero a junio por "
    "región del Excel que acabas de hacer"
)


def _maker(db_engine):
    engine, _ = db_engine
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _mundo(maker) -> dict:
    sufijo = uuid.uuid4().hex[:8]
    raiz = Path(tempfile.mkdtemp(prefix="ws-archivos-"))
    async with maker() as db:
        nodo = Node(name=f"nodo-{sufijo}", hostname="localhost")
        ana = User(username=f"ana-{sufijo}", display_name="Ana", password_hash="x", is_active=True)
        beto = User(username=f"beto-{sufijo}", display_name="Beto", password_hash="x", is_active=True)
        db.add_all([nodo, ana, beto])
        await db.flush()
        agente = Agent(
            node_id=nodo.id, name=f"op-{sufijo}", slug=f"op-{sufijo}", workspace_path=str(raiz / "op"), status="running"
        )
        otro = Agent(
            node_id=nodo.id,
            name=f"otro-{sufijo}",
            slug=f"otro-{sufijo}",
            workspace_path=str(raiz / "otro"),
            status="running",
        )
        db.add_all([agente, otro])
        await db.flush()
        db.add_all([AgentAssignment(agent_id=ag.id, user_id=u.id) for ag in (agente, otro) for u in (ana, beto)])
        e = {"agente": agente, "otro": otro, "ana": ana, "beto": beto, "raiz": raiz}
        claves = {"k1": (agente, ana), "k2": (agente, ana), "kb": (agente, beto), "ko": (otro, ana)}
        for nombre, (ag, usuario) in claves.items():
            e[nombre] = PublicChatApiKey(
                key_hash=f"hash-{nombre}-{sufijo}",
                key_prefix="pk_",
                label=f"bridge-teams-{nombre}",
                agent_id=ag.id,
                allowed_domains=["*"],
                bound_user_id=usuario.id,
                last_notification_poll_at=datetime.now(UTC),
            )
            db.add(e[nombre])
        await db.flush()
        for nombre, clave in (("s1", "k1"), ("s1b", "k1"), ("s2", "k2"), ("sb", "kb"), ("so", "ko")):
            e[nombre] = PublicChatSession(
                api_key_id=e[clave].id,
                agent_id=e[clave].agent_id,
                session_token=_hash_key(f"token-{nombre}"),
                status="active",
                last_activity=datetime.now(UTC),
                ttl_minutes=60,
            )
            db.add(e[nombre])
        await db.commit()
    return e


def _archivo(agente: Agent, extension: str = ".xlsx", crear: bool = True) -> str:
    relativa = f"uploads/{uuid.uuid4()}{extension}"
    if crear:
        destino = Path(agente.workspace_path) / relativa
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"PK\x03\x04contenido")
    return relativa


async def _trabajo_previo(
    maker,
    e,
    ruta: str,
    sesion="s1",
    clave="k1",
    usuario="ana",
    agente="agente",
    minutos=10,
    sesion_en_metadata=True,
    **extra,
) -> Task:
    metadata = {
        "background_job": True,
        "source": "background_job",
        "thread_user_id": e[usuario].id,
        "pedido_largo": True,
        "public_chat_api_key_id": e[clave].id,
        **({"origen_public_session_id": e[sesion].id} if sesion_en_metadata else {}),
        **extra,
    }
    async with maker() as db:
        tarea = Task(
            agent_id=e[agente].id,
            title="Trabajo en segundo plano: créame un Excel con ventas mensuales",
            prompt="Pedido del usuario:\ncréame un Excel con ventas mensuales por región y un gráfico",
            status="completed",
            response=f"Listo, generé el Excel con las ventas mensuales y el gráfico.\n\nMEDIA: {ruta}",
            queued_at=datetime.now(UTC) - timedelta(minutes=minutos),
            completed_at=datetime.now(UTC) - timedelta(minutes=minutos - 1),
            metadata_json=metadata,
        )
        db.add(tarea)
        await db.commit()
        return tarea


def _servicio(maker) -> PublicChatService:
    return PublicChatService(maker, SimpleNamespace(active_tasks={}, submit_task=AsyncMock()), None)


async def _enviar_largo(maker, e, sesion: str, texto: str = PEDIDO_TURNO_2) -> Task:
    async with maker() as db:
        task_id, respuesta = await _servicio(maker).send_message(e[sesion].id, f"token-{sesion}", texto, db)
    assert task_id is None and respuesta == AVISO_PEDIDO_LARGO, respuesta
    async with maker() as db:
        filas = await db.execute(
            select(Task)
            .where(
                Task.agent_id == e[sesion].agent_id,
                Task.title.like("Trabajo en segundo plano:%"),
                Task.status == "queued",
            )
            .order_by(Task.queued_at.desc())
        )
        trabajo = filas.scalars().first()
        prompt = trabajo.prompt
        trabajo.status = "cancelled"
        await db.commit()
        trabajo.prompt = prompt
        return trabajo


async def _enviar_corto(maker, e, sesion: str, texto: str) -> Task:
    async with maker() as db:
        task_id, _ = await _servicio(maker).send_message(e[sesion].id, f"token-{sesion}", texto, db)
    async with maker() as db:
        tarea = await db.get(Task, task_id)
        tarea.status = "completed"
        tarea.response = "Respuesta."
        tarea.completed_at = datetime.now(UTC)
        await db.commit()
        return tarea


@pytest.mark.integration
@requires_database
class TestTrabajoEnSegundoPlanoConoceLosArchivos:
    async def test_caso_real_el_informe_interactivo_recibe_el_excel_del_trabajo_anterior(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        previo = await _trabajo_previo(maker, e, ruta)
        trabajo = await _enviar_largo(maker, e, "s1")
        assert trabajo is not None
        assert ENCABEZADO in trabajo.prompt, trabajo.prompt
        assert f"{ruta} (tarea {previo.id[:8]})" in trabajo.prompt
        assert trabajo.prompt.startswith(DIRECTIVA_HTML), trabajo.prompt[:200]
        assert DIRECTIVA_EXCEL not in trabajo.prompt
        assert trabajo.prompt.index(ENCABEZADO) < trabajo.prompt.index("Pedido del usuario:")
        assert trabajo.metadata_json["origen_public_session_id"] == e["s1"].id
        assert "public_session_id" not in trabajo.metadata_json

    async def test_el_trabajo_nuevo_deja_su_sesion_y_el_siguiente_lo_encuentra(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        primero = await _enviar_largo(maker, e, "s1", "créame un Excel con ventas mensuales por región y un gráfico")
        ruta = _archivo(e["agente"])
        async with maker() as db:
            fila = await db.get(Task, primero.id)
            fila.status = "completed"
            fila.response = f"Listo.\nMEDIA: {ruta}"
            fila.completed_at = datetime.now(UTC)
            await db.commit()
        segundo = await _enviar_largo(maker, e, "s1")
        assert ruta in segundo.prompt and primero.id[:8] in segundo.prompt

    async def test_otro_usuario_otra_sesion_otra_clave_u_otro_agente_no_lo_reciben(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        await _trabajo_previo(maker, e, ruta)
        for sesion in ("sb", "s1b", "s2", "so"):
            trabajo = await _enviar_largo(maker, e, sesion)
            assert trabajo is not None, sesion
            assert ruta not in trabajo.prompt and ENCABEZADO not in trabajo.prompt, (sesion, trabajo.prompt)

    async def test_tareas_ajenas_no_se_filtran_a_esta_sesion(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ajenas = []
        for kwargs in (
            {"usuario": "beto", "clave": "kb", "sesion": "sb"},
            {"sesion": "s1b"},
            {"clave": "k2", "sesion": "s2"},
            {"agente": "otro", "clave": "ko", "sesion": "so"},
        ):
            agente = e[kwargs.get("agente", "agente")]
            ruta = _archivo(agente)
            ajenas.append(ruta)
            await _trabajo_previo(maker, e, ruta, **kwargs)
        trabajo = await _enviar_largo(maker, e, "s1")
        assert all(ruta not in trabajo.prompt for ruta in ajenas), trabajo.prompt
        assert ENCABEZADO not in trabajo.prompt

    async def test_tras_reset_no_se_ofrecen(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        await _trabajo_previo(maker, e, ruta)
        async with maker() as db:
            respuesta = await _servicio(maker).handle_cancel_request(e["s1"].id, "token-s1", "/reset", db)
        assert respuesta and "reiniciada" in respuesta
        trabajo = await _enviar_largo(maker, e, "s1")
        assert ruta not in trabajo.prompt and ENCABEZADO not in trabajo.prompt, trabajo.prompt

    async def test_reset_con_pedido_en_el_mismo_mensaje_no_los_ofrece(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        await _trabajo_previo(maker, e, ruta)
        async with maker() as db:
            assert (
                await _servicio(maker).handle_cancel_request(e["s1"].id, "token-s1", f"/reset {PEDIDO_TURNO_2}", db)
                is None
            )
        trabajo = await _enviar_largo(maker, e, "s1", f"/reset {PEDIDO_TURNO_2}")
        assert ruta not in trabajo.prompt and ENCABEZADO not in trabajo.prompt, trabajo.prompt

    async def test_archivo_inexistente_fuera_del_workspace_viejo_o_tarea_fallida_no_se_listan(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        inexistente = _archivo(e["agente"], crear=False)
        await _trabajo_previo(maker, e, inexistente, minutos=5)
        fuera = Path(e["raiz"]) / "secreto.xlsx"
        fuera.write_bytes(b"x")
        await _trabajo_previo(maker, e, "../secreto.xlsx", minutos=6)
        await _trabajo_previo(maker, e, str(fuera), minutos=7)
        viejo = _archivo(e["agente"])
        minutos_viejo = int(rc.VENTANA_IMPLICITA.total_seconds() // 60) + 5
        await _trabajo_previo(maker, e, viejo, minutos=minutos_viejo)
        fallida = _archivo(e["agente"])
        tarea_fallida = await _trabajo_previo(maker, e, fallida, minutos=8)
        async with maker() as db:
            fila = await db.get(Task, tarea_fallida.id)
            fila.status = "failed"
            await db.commit()
        sin_sesion = _archivo(e["agente"])
        await _trabajo_previo(maker, e, sin_sesion, minutos=9, sesion_en_metadata=False)
        trabajo = await _enviar_largo(maker, e, "s1")
        for ruta in (inexistente, "secreto.xlsx", viejo, fallida, sin_sesion):
            assert ruta not in trabajo.prompt, (ruta, trabajo.prompt)
        assert ENCABEZADO not in trabajo.prompt

    async def test_mas_reciente_primero_y_tope_de_diez(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        rutas = []
        for minutos in range(30, 18, -1):
            ruta = _archivo(e["agente"])
            rutas.append(ruta)
            await _trabajo_previo(maker, e, ruta, minutos=minutos)
        trabajo = await _enviar_largo(maker, e, "s1")
        recientes = list(reversed(rutas))[:10]
        posiciones = [trabajo.prompt.index(ruta) for ruta in recientes]
        assert posiciones == sorted(posiciones)
        assert all(ruta not in trabajo.prompt for ruta in rutas[:2])


@pytest.mark.integration
@requires_database
class TestChatNormalYContinuacion:
    async def test_el_chat_normal_tambien_recibe_los_archivos_del_trabajo_en_segundo_plano(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        await _trabajo_previo(maker, e, ruta)
        tarea = await _enviar_corto(maker, e, "s1", "¿cuántas hojas tiene el archivo que hiciste?")
        assert ENCABEZADO in tarea.prompt and ruta in tarea.prompt, tarea.prompt
        ajena = await _enviar_corto(maker, e, "sb", "¿cuántas hojas tiene el archivo que hiciste?")
        assert ruta not in ajena.prompt

    async def test_el_chat_normal_ve_archivos_de_turnos_de_chat_con_su_nombre(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"], ".pdf")
        file_id = Path(ruta).stem
        async with maker() as db:
            db.add(
                Task(
                    agent_id=e["agente"].id,
                    title="Public chat message",
                    prompt="hazme el acta en pdf",
                    status="completed",
                    response=f"Aquí está.\nMEDIA: {ruta}",
                    queued_at=datetime.now(UTC) - timedelta(minutes=4),
                    completed_at=datetime.now(UTC),
                    metadata_json={
                        "conversation": True,
                        "source": "public_chat",
                        "public_session_id": e["s1"].id,
                        "thread_user_id": e["ana"].id,
                        "public_chat_api_key_id": e["k1"].id,
                        "response_attachments": [{"file_id": file_id, "filename": "acta_comite.pdf"}],
                    },
                )
            )
            await db.commit()
        tarea = await _enviar_corto(maker, e, "s1", "resume el pdf que hiciste en 3 puntos")
        assert f"{ruta} («acta_comite.pdf», tarea" in tarea.prompt, tarea.prompt

    async def test_la_continuacion_automatica_tambien_los_recibe(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        ruta = _archivo(e["agente"])
        await _trabajo_previo(maker, e, ruta)
        async with maker() as db:
            cortada = Task(
                agent_id=e["agente"].id,
                title="Public chat message",
                prompt="hazme un análisis del archivo de ventas que generaste por región y mes",
                status="failed",
                queued_at=datetime.now(UTC) - timedelta(minutes=2),
                metadata_json={
                    "conversation": True,
                    "source": "public_chat",
                    "public_session_id": e["s1"].id,
                    "thread_user_id": e["ana"].id,
                    "public_chat_api_key_id": e["k1"].id,
                },
            )
            db.add(cortada)
            await db.flush()
            job = await auto_background.create_continuation(db, cortada, "exceeded the task timeout")
            await db.commit()
        assert job is not None
        assert ENCABEZADO in job.prompt and ruta in job.prompt, job.prompt
        assert job.metadata_json["origen_public_session_id"] == e["s1"].id


def test_linea_vacia_sin_archivos():
    assert linea_archivos([]) is None
    linea = linea_archivos([{"ruta": "uploads/a.xlsx", "task_id": "23ac9592-0000", "nombre": None}])
    assert linea.startswith(ENCABEZADO) and "uploads/a.xlsx (tarea 23ac9592)" in linea
