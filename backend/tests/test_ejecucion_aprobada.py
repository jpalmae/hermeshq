import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac
from hermeshq.services import conciliacion_graph as cg
from hermeshq.services import ejecucion_aprobada as ej
from hermeshq.services import graph_central as gc

pytestmark = [pytest.mark.integration, requires_database]

BASE = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
CTX = ac.Contexto(agent_id="ag1", user_id="u1", conversacion_id="conv-1", task_id="t1", canal="web")


def _evento(asunto="Revisión", inicio="2026-11-25T15:00:00"):
    return {
        "metodo": "POST",
        "ruta": "/me/events",
        "cuerpo": {
            "subject": asunto,
            "start": {"dateTime": inicio, "timeZone": "UTC"},
            "end": {"dateTime": inicio.replace("15:", "16:"), "timeZone": "UTC"},
        },
    }


TEAMS = {
    "metodo": "POST",
    "ruta": "/me/chats/19:chat-1@thread.v2/messages",
    "cuerpo": {"body": {"contentType": "text", "content": "Hola"}},
    "miembros": ["ana@empresa.cl", "bruno@empresa.cl"],
}


class _Graph:
    def __init__(self):
        self.llamadas: list[tuple] = []
        self.estado = 200
        self.cuerpo: dict = {"id": "evt-1", "subject": "Revisión"}
        self.error: Exception | None = None

    def __call__(self, token, canon, aprobacion_id=None, archivo=None):
        self.llamadas.append((token, canon, aprobacion_id))
        if self.error is not None:
            raise self.error
        return self.estado, dict(self.cuerpo)


@pytest.fixture
def graph(monkeypatch):
    fake = _Graph()
    monkeypatch.setattr(gc, "ejecutar", fake)
    monkeypatch.setattr(ej, "_token", _token_falso)
    monkeypatch.setattr(ac, "central_activa_para", lambda agente: True)
    monkeypatch.setattr(gc, "miembros_del_chat", lambda token, ruta: ["ana@empresa.cl", "bruno@empresa.cl"])
    return fake


async def _token_falso(db, usuario_id):
    return "token-del-servidor"


@pytest.fixture
async def mundo(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        nodo = Node(id="nodo-ej", name="nodo-ej", hostname="localhost")
        s.add(nodo)
        await s.flush()
        s.add(User(id="u1", username="bruno", display_name="bruno", password_hash="x"))
        s.add(Agent(id="ag1", node_id=nodo.id, name="ag1", slug="ag1", workspace_path="/tmp/ag1"))
        await s.flush()
        s.add(AgentAssignment(id="asig-1", agent_id="ag1", user_id="u1"))
        await s.commit()
    yield engine, maker


async def _crear(db, contenido, tipo=ac.TIPO_CALENDARIO, ahora=BASE, ttl=None):
    return await ac.crear(db, tipo, contenido, CTX, ahora=ahora, ttl=ttl)


async def _fila(db, aprob_id):
    return await ac._cargar(db, aprob_id)


async def test_aprobar_ejecuta_en_servidor_con_el_contenido_aprobado(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        decidida = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert len(graph.llamadas) == 1
    token, canon, aprobacion_id = graph.llamadas[0]
    assert token == "token-del-servidor" and aprobacion_id == aprob.id
    assert canon["cuerpo"].pop("transactionId") == f"hermeshq-{aprob.id}"
    assert canon == aprob.contenido_json
    assert (decidida.estado, decidida.envio_estado) == ("ejecutada", "confirmada")
    assert decidida.envio_proveedor_id == "evt-1"
    assert decidida.resultado_json["respuesta"]["id"] == "evt-1"


async def test_aprobar_dos_veces_o_ejecutar_otra_vez_no_repite_la_escritura(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    async with maker() as db:
        with pytest.raises(ac.ErrorAprobacion):
            await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
        await ej.ejecutar(db, aprob.id, ahora=BASE)
    assert len(graph.llamadas) == 1


async def test_dos_workers_compiten_y_solo_uno_ejecuta(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
    async with maker() as a, maker() as b:
        await asyncio.gather(ej.ejecutar(a, aprob.id, ahora=BASE), ej.ejecutar(b, aprob.id, ahora=BASE))
    assert len(graph.llamadas) == 1
    async with maker() as db:
        assert (await _fila(db, aprob.id)).estado == "ejecutada"


async def test_reinicio_tras_aprobar_el_barrido_ejecuta_una_sola_vez(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
    assert graph.llamadas == []
    cuenta = await ej.barrido(maker, ahora=BASE + timedelta(minutes=1))
    assert cuenta["ejecutadas"] == 1 and len(graph.llamadas) == 1
    await ej.barrido(maker, ahora=BASE + timedelta(minutes=2))
    assert len(graph.llamadas) == 1


async def test_modelo_repite_la_misma_accion_y_no_se_pide_otra_aprobacion(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    async with maker() as db:
        repetida = await _crear(db, _evento())
        assert repetida.id == aprob.id and repetida.estado == "ejecutada"
    assert len(graph.llamadas) == 1


async def test_argumentos_cambiados_piden_aprobacion_nueva_y_no_tocan_la_ejecutada(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    async with maker() as db:
        distinta = await _crear(db, _evento(inicio="2026-11-26T15:00:00"))
        assert distinta.id != aprob.id and distinta.estado == "pendiente"
        original = await _fila(db, aprob.id)
        assert original.estado == "ejecutada"
    assert len(graph.llamadas) == 1


async def test_rechazo_no_ejecuta_nada(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        rechazada = await ac.decidir(db, aprob.id, False, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert rechazada.estado == "rechazada"
    assert await ej.barrido(maker, ahora=BASE + timedelta(minutes=5)) == {
        "ejecutadas": 0,
        "conciliadas": 0,
        "expiradas": 0,
    }
    assert graph.llamadas == []


async def test_politica_bloqueada_tras_aprobar_cancela_la_ejecucion(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
        await db.execute(
            Agent.__table__.update().where(Agent.id == "ag1").values(approval_policy={"calendario.crear": "bloqueada"})
        )
        await db.commit()
    await ej.barrido(maker, ahora=BASE + timedelta(minutes=1))
    assert graph.llamadas == []
    async with maker() as db:
        assert (await _fila(db, aprob.id)).estado == "expirada"


async def test_asignacion_revocada_tras_aprobar_cancela_la_ejecucion(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
        await db.execute(AgentAssignment.__table__.delete())
        await db.commit()
    await ej.barrido(maker, ahora=BASE + timedelta(minutes=1))
    assert graph.llamadas == []
    async with maker() as db:
        assert (await _fila(db, aprob.id)).estado == "expirada"


async def test_aprobacion_vencida_antes_de_ejecutar_no_se_ejecuta(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento(), ttl=timedelta(minutes=10))
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
    await ej.barrido(maker, ahora=BASE + timedelta(hours=1))
    assert graph.llamadas == []
    async with maker() as db:
        assert (await _fila(db, aprob.id)).estado == "expirada"


async def test_sin_token_del_servidor_no_ejecuta_y_expira(mundo, graph, monkeypatch):
    engine, maker = mundo

    async def sin_token(db, usuario_id):
        raise ac.ErrorAprobacion("sin_token_m365")

    monkeypatch.setattr(ej, "_token", sin_token)
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
        fila = await _fila(db, aprob.id)
    assert graph.llamadas == []
    assert fila.estado == "expirada"


async def test_error_definitivo_de_graph_deja_la_aprobacion_fallida(mundo, graph):
    engine, maker = mundo
    graph.estado = 400
    graph.cuerpo = {"error": {"message": "Recurso no válido"}}
    async with maker() as db:
        aprob = await _crear(db, _evento())
        final = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert len(graph.llamadas) == 1
    assert (final.estado, final.envio_estado) == ("fallida", "fallida")


async def test_timeout_remoto_queda_incierto_y_no_se_reintenta_sin_conciliar(mundo, graph, monkeypatch):
    engine, maker = mundo
    graph.error = TimeoutError("sin respuesta de Graph")

    async def no_determinable(db, aprob_id, token, resolutor=cg.RESOLUTOR_AUTOMATICO):
        raise ac.ErrorAprobacion("conciliacion_manual_requerida")

    monkeypatch.setattr(cg, "conciliar_automatico", no_determinable)
    async with maker() as db:
        aprob = await _crear(db, _evento())
        final = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert (final.estado, final.envio_estado) == ("ejecutando", "desconocida")
    await ej.barrido(maker, ahora=BASE + timedelta(hours=1))
    assert len(graph.llamadas) == 1
    async with maker() as db:
        assert (await _fila(db, aprob.id)).envio_estado == "desconocida"


async def test_resultado_incierto_aplicado_se_concilia_sin_repetir(mundo, graph, monkeypatch):
    engine, maker = mundo
    graph.error = TimeoutError("sin respuesta de Graph")

    async def aplicado(db, aprob_id, token, resolutor=cg.RESOLUTOR_AUTOMATICO):
        from hermeshq.services import aprobacion_central as acm

        final = await acm.conciliar_envio(db, aprob_id, lambda _h: "evt-aplicado")
        return final, cg.APLICADA, "evt-aplicado"

    monkeypatch.setattr(cg, "conciliar_automatico", aplicado)
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    await ej.barrido(maker, ahora=BASE + timedelta(hours=1))
    assert len(graph.llamadas) == 1
    async with maker() as db:
        fila = await _fila(db, aprob.id)
    assert (fila.estado, fila.envio_estado) == ("ejecutada", "confirmada")


async def test_teams_con_archivo_queda_en_la_via_del_plugin(mundo, graph):
    engine, maker = mundo
    contenido = dict(TEAMS, archivo={"nombre": "a.pdf", "sha256": "a" * 64, "bytes": 10})
    async with maker() as db:
        aprob = await _crear(db, contenido, tipo=ac.TIPO_TEAMS)
        decidida = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert decidida.estado == "aprobada"
    assert graph.llamadas == []


async def test_teams_texto_se_ejecuta_en_servidor(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, TEAMS, tipo=ac.TIPO_TEAMS)
        decidida = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert decidida.estado == "ejecutada"
    assert len(graph.llamadas) == 1


async def test_correo_no_entra_en_la_ejecucion_del_servidor(mundo, graph):
    engine, maker = mundo
    contenido = {
        "cuenta_emisora": "m365:u1",
        "para": ["ana@empresa.cl"],
        "cc": [],
        "cco": [],
        "asunto": "Hola",
        "cuerpo_html": "<p>Hola</p>",
        "firma": "",
        "firma_sha256": "",
        "adjuntos": [],
    }
    async with maker() as db:
        aprob = await _crear(db, contenido, tipo=ac.TIPO_CORREO)
        decidida = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert decidida.estado == "aprobada"
    assert graph.llamadas == []


async def test_otro_usuario_no_puede_aprobar_ni_disparar_la_ejecucion(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        with pytest.raises(ac.ErrorAprobacion):
            await ac.decidir(db, aprob.id, True, "otro", "web", aprob.huella, "conv-1", ahora=BASE)
    assert graph.llamadas == []
    async with maker() as db:
        assert (await _fila(db, aprob.id)).estado == "pendiente"


async def test_el_barrido_no_toca_aprobaciones_recientes(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE, ejecutar_al_aprobar=False)
    cuenta = await ej.barrido(maker, ahora=BASE)
    assert cuenta["ejecutadas"] == 0 and graph.llamadas == []


async def test_registro_guarda_solo_el_resumen_de_la_respuesta(mundo, graph):
    engine, maker = mundo
    graph.cuerpo = {"id": "evt-9", "subject": "Revisión", "body": {"content": "texto largo que no debe guardarse"}}
    async with maker() as db:
        aprob = await _crear(db, _evento())
        final = await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    assert "body" not in final.resultado_json["respuesta"]
    assert final.resultado_json["respuesta"]["id"] == "evt-9"
    assert final.envio_proveedor_id == "evt-9"


async def test_aviso_pendiente_se_entrega_una_sola_vez(mundo, graph):
    engine, maker = mundo
    async with maker() as db:
        aprob = await _crear(db, _evento())
        await ac.decidir(db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=BASE)
    async with maker() as db:
        texto = await ej.avisos_pendientes(db, "ag1", "u1", "conv-1")
        await db.commit()
    assert texto and ej.MARCA_EJECUTADA in texto
    async with maker() as db:
        assert await ej.avisos_pendientes(db, "ag1", "u1", "conv-1") is None
    async with maker() as db:
        assert await ej.avisos_pendientes(db, "ag1", "u1", "otra-conversacion") is None
