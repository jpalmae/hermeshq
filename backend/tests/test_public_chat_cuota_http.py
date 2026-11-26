from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from conftest import requires_database
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey
from hermeshq.models.user import User
from hermeshq.routers import public_chat
from hermeshq.services.public_chat_service import PublicChatService, SimpleRateLimiter, _hash_key

pytestmark = [pytest.mark.integration, requires_database]

IP_TUNEL = "127.0.0.1"
IP_ATACANTE = "203.0.113.9"
CANAL_1, CANAL_2 = "teams-principal", "teams-2"
USUARIOS = 31


class _Broker:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.handlers = []

    def subscribe(self, handler):
        self.handlers.append(handler)
        for task_id in list(self.supervisor.enviadas):
            asyncio.get_running_loop().create_task(
                handler({"type": "task.completed", "task_id": task_id, "response": "ok", "metadata": {}})
            )

    def unsubscribe(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)


class _Supervisor:
    def __init__(self, maker):
        self.session_factory = maker
        self.enviadas: list[str] = []
        self.active_tasks: dict = {}

    async def submit_task(self, task_id):
        self.enviadas.append(task_id)


async def _crear_mundo(maker, raiz) -> dict:
    m: dict = {"usuarios": [], "claves": {}, "id_clave": {}, "sesiones": {}}
    async with maker() as s:
        nodo = Node(name="nodo-cuotas", hostname="localhost")
        s.add(nodo)
        await s.flush()
        espacio = raiz / "agente"
        espacio.mkdir(parents=True, exist_ok=True)
        agente = Agent(
            node_id=nodo.id,
            name="agente-cuotas",
            slug=f"agente-cuotas-{uuid.uuid4().hex[:6]}",
            workspace_path=str(espacio),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=[],
            max_iterations=2,
        )
        s.add(agente)
        c1 = BotChannel(slug=CANAL_1, display_name="Teams principal", is_default=True)
        c2 = BotChannel(slug=CANAL_2, display_name="Teams 2", azure_app_id="app-bot-2", tenant_id="tenant-1")
        s.add_all([c1, c2])
        await s.flush()
        m["agente"], m["canales"] = agente.id, {1: c1.id, 2: c2.id}
        for n in range(USUARIOS):
            usuario = User(
                username=f"cuota-{n}-{uuid.uuid4().hex[:6]}",
                display_name=f"Usuario {n}",
                password_hash="x",
                role="user",
                teams_id=f"29:u{n}-b1",
            )
            s.add(usuario)
            await s.flush()
            m["usuarios"].append(usuario.id)
            s.add(AgentAssignment(user_id=usuario.id, agent_id=agente.id))
            s.add(
                UserChannelIdentity(
                    user_id=usuario.id,
                    bot_channel_id=c2.id,
                    external_user_id=f"29:u{n}-b2",
                    aad_object_id=f"aad-u{n}",
                    tenant_id="tenant-1",
                    verified_at=datetime.now(UTC),
                )
            )

        def clave(nombre, *, bot, usuario=None, confianza=False, etiqueta="bridge-teams"):
            cruda = f"hqk-{uuid.uuid4().hex}"
            fila = PublicChatApiKey(
                key_hash=_hash_key(cruda),
                key_prefix=cruda[:8],
                label=etiqueta,
                agent_id=agente.id,
                allowed_domains=["*"],
                is_active=True,
                bound_user_id=usuario,
                trust_sender_id=confianza,
                bot_channel_id=m["canales"][bot] if bot == 2 else None,
            )
            s.add(fila)
            m["claves"][nombre] = cruda
            return fila

        filas = {"confianza-b1": clave("confianza-b1", bot=1, confianza=True)}
        filas["confianza-b2"] = clave("confianza-b2", bot=2, confianza=True, etiqueta="bridge-teams-2")
        filas["widget"] = clave("widget", bot=1, etiqueta="widget-web")
        for n, user_id in enumerate(m["usuarios"]):
            filas[f"fija-{n}"] = clave(f"fija-{n}", bot=1, usuario=user_id)
        await s.flush()
        m["id_clave"] = {nombre: fila.id for nombre, fila in filas.items()}
        await s.commit()
    return m


@pytest.fixture
async def entorno(db_engine, tmp_path):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    mundo = await _crear_mundo(maker, tmp_path)
    supervisor = _Supervisor(maker)
    broker = _Broker(supervisor)
    app = FastAPI()
    app.include_router(public_chat.public_router)

    async def _db():
        async with maker() as sesion:
            yield sesion

    app.dependency_overrides[get_db_session] = _db
    app.state.public_chat_service = PublicChatService(maker, supervisor, broker)
    app.state.event_broker = broker
    app.state.supervisor = supervisor
    clientes = {}

    def cliente(ip=IP_TUNEL):
        if ip not in clientes:
            transporte = httpx.ASGITransport(app=app, client=(ip, 40000))
            clientes[ip] = httpx.AsyncClient(transport=transporte, base_url="http://hq", timeout=60)
        return clientes[ip]

    yield {"m": mundo, "app": app, "servicio": app.state.public_chat_service, "cliente": cliente}
    for c in clientes.values():
        await c.aclose()


def _cabeceras(clave, canal=None, **extra):
    h = {"X-Api-Key": clave, **extra}
    if canal:
        h["X-Bot-Channel"] = canal
    return h


async def _sesion(e, nombre_clave, *, remitente=None, canal=None, ip=IP_TUNEL, **extra):
    cuerpo = {"sender_id": remitente} if remitente else {}
    return await e["cliente"](ip).post(
        "/api/public/chat/sessions", headers=_cabeceras(e["m"]["claves"][nombre_clave], canal, **extra), json=cuerpo
    )


async def _abrir(e, nombre_clave, **kw):
    r = await _sesion(e, nombre_clave, **kw)
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["session_token"]


async def _mensaje(e, sesion, texto="hola", *, ip=IP_TUNEL, **cabeceras):
    session_id, token = sesion
    return await e["cliente"](ip).post(
        f"/api/public/chat/sessions/{session_id}/messages",
        headers={"X-Session-Token": token, **cabeceras},
        json={"content": texto},
    )


def _eventos(r):
    return [json.loads(linea[6:]) for linea in r.text.splitlines() if linea.startswith("data: ")]


async def _agotar(e, sesion, veces, **kw):
    codigos = []
    for _ in range(veces):
        codigos.append((await _mensaje(e, sesion, **kw)).status_code)
    return codigos


class TestTreintaUsuariosPorElMismoBridge:
    async def test_clave_de_confianza_treinta_usuarios_simultaneos_ninguno_limitado(self, entorno):
        e = entorno
        sesiones = await asyncio.gather(
            *[_sesion(e, "confianza-b1", remitente=f"29:u{n}-b1", canal=CANAL_1) for n in range(30)]
        )
        assert [r.status_code for r in sesiones] == [200] * 30, [r.text for r in sesiones if r.status_code != 200]
        pares = [(r.json()["session_id"], r.json()["session_token"]) for r in sesiones]
        respuestas = await asyncio.gather(*[_mensaje(e, par) for par in pares])
        assert [r.status_code for r in respuestas] == [200] * 30, [r.text for r in respuestas if r.status_code != 200]
        assert all(_eventos(r)[-1]["type"] == "done" for r in respuestas)

    async def test_claves_fijas_treinta_usuarios_simultaneos_ninguno_limitado(self, entorno):
        e = entorno
        sesiones = await asyncio.gather(*[_sesion(e, f"fija-{n}", canal=CANAL_1) for n in range(30)])
        assert [r.status_code for r in sesiones] == [200] * 30, [r.text for r in sesiones if r.status_code != 200]
        pares = [(r.json()["session_id"], r.json()["session_token"]) for r in sesiones]
        respuestas = await asyncio.gather(*[_mensaje(e, par) for par in pares])
        assert [r.status_code for r in respuestas] == [200] * 30, [r.text for r in respuestas if r.status_code != 200]


class TestUsuarioQueExcedeSuCuota:
    async def test_solo_el_recibe_429_y_los_demas_siguen(self, entorno):
        e = entorno
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        beto = await _abrir(e, "confianza-b1", remitente="29:u1-b1")
        codigos = await _agotar(e, ana, 21)
        assert codigos[:20] == [200] * 20
        assert codigos[20] == 429
        assert (await _mensaje(e, beto)).status_code == 200
        otros = await asyncio.gather(
            *[_mensaje(e, await _abrir(e, "confianza-b1", remitente=f"29:u{n}-b1")) for n in range(2, 12)]
        )
        assert [r.status_code for r in otros] == [200] * 10
        assert (await _mensaje(e, ana)).status_code == 429

    async def test_una_segunda_sesion_del_mismo_usuario_no_le_da_cuota_nueva(self, entorno):
        e = entorno
        primera = await _abrir(e, "fija-0")
        assert await _agotar(e, primera, 20) == [200] * 20
        segunda = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        assert (await _mensaje(e, segunda)).status_code == 429
        assert (await _mensaje(e, await _abrir(e, "fija-1"))).status_code == 200


class TestDosBots:
    async def test_cuota_agotada_en_el_bot1_no_afecta_al_bot2(self, entorno):
        e = entorno
        en_bot1 = await _abrir(e, "confianza-b1", remitente="29:u0-b1", canal=CANAL_1)
        en_bot2 = await _abrir(e, "confianza-b2", remitente="29:u0-b2", canal=CANAL_2)
        assert (await _agotar(e, en_bot1, 21))[20] == 429
        assert await _agotar(e, en_bot2, 20) == [200] * 20
        assert (await _mensaje(e, en_bot2)).status_code == 429
        otro_en_bot2 = await _abrir(e, "confianza-b2", remitente="29:u1-b2", canal=CANAL_2)
        assert (await _mensaje(e, otro_en_bot2)).status_code == 200

    async def test_treinta_usuarios_repartidos_en_dos_bots(self, entorno):
        e = entorno
        sesiones = await asyncio.gather(
            *[
                _sesion(e, "confianza-b1", remitente=f"29:u{n}-b1", canal=CANAL_1)
                if n % 2
                else _sesion(e, "confianza-b2", remitente=f"29:u{n}-b2", canal=CANAL_2)
                for n in range(30)
            ]
        )
        assert [r.status_code for r in sesiones] == [200] * 30
        respuestas = await asyncio.gather(
            *[_mensaje(e, (r.json()["session_id"], r.json()["session_token"])) for r in sesiones]
        )
        assert [r.status_code for r in respuestas] == [200] * 30


class TestSinAutenticar:
    async def test_clave_invalida_se_limita_por_ip_sin_tocar_cuotas(self, entorno):
        e = entorno
        codigos = []
        for _ in range(25):
            r = await e["cliente"](IP_ATACANTE).post(
                "/api/public/chat/sessions", headers={"X-Api-Key": "hqk-falsa"}, json={"sender_id": "29:u0-b1"}
            )
            codigos.append(r.status_code)
        assert codigos[:20] == [401] * 20
        assert codigos[20:] == [429] * 5
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        assert await _agotar(e, ana, 20) == [200] * 20
        assert (await _mensaje(e, ana)).status_code == 429

    async def test_token_de_sesion_falso_se_limita_por_ip_y_no_gasta_cuota_ajena(self, entorno):
        e = entorno
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        falsos = [(await _mensaje(e, (ana[0], f"token-falso-{n}"), ip=IP_ATACANTE)).status_code for n in range(22)]
        assert falsos[:20] == [400] * 20
        assert falsos[20:] == [429, 429]
        assert await _agotar(e, ana, 20) == [200] * 20
        assert (await _mensaje(e, ana)).status_code == 429

    async def test_ip_bloqueada_no_alcanza_al_bridge_de_otra_ip(self, entorno):
        e = entorno
        for _ in range(21):
            await e["cliente"](IP_ATACANTE).get("/api/public/chat/notifications", headers={"X-Api-Key": "mala"})
        r = await e["cliente"](IP_ATACANTE).get(
            "/api/public/chat/notifications", headers={"X-Api-Key": e["m"]["claves"]["fija-0"]}
        )
        assert r.status_code == 429
        r = await e["cliente"]().get(
            "/api/public/chat/notifications", headers={"X-Api-Key": e["m"]["claves"]["fija-0"]}
        )
        assert r.status_code == 200

    async def test_x_real_ip_rotativa_desde_ip_no_confiable_no_evade(self, entorno):
        e = entorno
        codigos = []
        for n in range(22):
            r = await e["cliente"](IP_ATACANTE).post(
                "/api/public/chat/sessions", headers={"X-Api-Key": "mala", "X-Real-IP": f"10.1.1.{n}"}, json={}
            )
            codigos.append(r.status_code)
        assert codigos[20:] == [429, 429]


class TestCabecerasFalsificadas:
    async def test_x_real_ip_distinta_por_mensaje_no_evade_la_cuota(self, entorno):
        e = entorno
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1", **{"X-Real-IP": "10.50.0.1"})
        codigos = [(await _mensaje(e, ana, **{"X-Real-IP": f"10.50.{n}.{n}"})).status_code for n in range(21)]
        assert codigos[:20] == [200] * 20
        assert codigos[20] == 429

    async def test_x_bot_channel_de_otro_bot_no_mueve_la_cuota(self, entorno):
        e = entorno
        r = await _sesion(e, "confianza-b1", remitente="29:u0-b1", canal=CANAL_2)
        assert r.status_code == 403
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1", canal=CANAL_1)
        codigos = [(await _mensaje(e, ana, **{"X-Bot-Channel": CANAL_2})).status_code for _ in range(21)]
        assert codigos[20] == 429
        en_bot2 = await _abrir(e, "confianza-b2", remitente="29:u0-b2", canal=CANAL_2)
        assert (await _mensaje(e, en_bot2)).status_code == 200

    async def test_sender_id_ajeno_con_clave_fija_gasta_la_cuota_del_dueno(self, entorno):
        e = entorno
        suplantada = await _abrir(e, "fija-0", remitente="29:u1-b1")
        assert (await _agotar(e, suplantada, 21))[20] == 429
        beto = await _abrir(e, "fija-1")
        assert await _agotar(e, beto, 20) == [200] * 20
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        assert (await _mensaje(e, ana)).status_code == 429

    async def test_cuerpo_del_mensaje_no_cambia_la_identidad(self, entorno):
        e = entorno
        ana = await _abrir(e, "confianza-b1", remitente="29:u0-b1")
        session_id, token = ana
        codigos = []
        for _ in range(21):
            r = await e["cliente"]().post(
                f"/api/public/chat/sessions/{session_id}/messages",
                headers={"X-Session-Token": token},
                json={"content": "hola", "sender_id": "29:u1-b1", "user_id": e["m"]["usuarios"][1]},
            )
            codigos.append(r.status_code)
        assert codigos[20] == 429
        beto = await _abrir(e, "confianza-b1", remitente="29:u1-b1")
        assert (await _mensaje(e, beto)).status_code == 200


class TestLimiteGlobalPorBot:
    async def test_el_bot_tiene_tope_global_y_el_otro_bot_sigue(self, entorno):
        e = entorno
        e["servicio"].bot_rate_limiter = SimpleRateLimiter(max_requests=5, window_seconds=60)
        sesiones = [await _abrir(e, "confianza-b1", remitente=f"29:u{n}-b1") for n in range(6)]
        codigos = [(await _mensaje(e, s)).status_code for s in sesiones]
        assert codigos == [200] * 5 + [429]
        en_bot2 = await _abrir(e, "confianza-b2", remitente="29:u0-b2", canal=CANAL_2)
        assert (await _mensaje(e, en_bot2)).status_code == 200

    async def test_claves_fijas_del_mismo_bot_suman_al_mismo_tope(self, entorno):
        e = entorno
        e["servicio"].bot_rate_limiter = SimpleRateLimiter(max_requests=3, window_seconds=60)
        sesiones = [await _abrir(e, f"fija-{n}") for n in range(4)]
        codigos = [(await _mensaje(e, s)).status_code for s in sesiones]
        assert codigos == [200, 200, 200, 429]

    async def test_tope_global_de_sesiones_nuevas(self, entorno):
        e = entorno
        e["servicio"].bot_session_limiter = SimpleRateLimiter(max_requests=4, window_seconds=60)
        codigos = [(await _sesion(e, "confianza-b1", remitente=f"29:u{n}-b1")).status_code for n in range(5)]
        assert codigos == [200] * 4 + [429]
        assert (await _sesion(e, "confianza-b2", remitente="29:u0-b2", canal=CANAL_2)).status_code == 200


class TestCompatibilidad:
    async def test_widget_anonimo_sigue_limitado_por_sesion_y_por_ip(self, entorno):
        e = entorno
        primera = await _abrir(e, "widget", ip="198.51.100.20")
        codigos = await _agotar(e, primera, 11, ip="198.51.100.20")
        assert codigos[:10] == [200] * 10
        assert codigos[10] == 429
        segunda = await _abrir(e, "widget", ip="198.51.100.20")
        restantes = await _agotar(e, segunda, 11, ip="198.51.100.20")
        assert restantes[:10] == [200] * 10
        assert restantes[10] == 429
        tercera = await _abrir(e, "widget", ip="198.51.100.20")
        assert (await _mensaje(e, tercera, ip="198.51.100.20")).status_code == 429
        otra_ip = await _abrir(e, "widget", ip="198.51.100.21")
        assert (await _mensaje(e, otra_ip, ip="198.51.100.21")).status_code == 200

    async def test_widget_anonimo_conserva_cinco_sesiones_por_ip(self, entorno):
        e = entorno
        codigos = [(await _sesion(e, "widget", ip="198.51.100.30")).status_code for _ in range(6)]
        assert codigos == [200] * 5 + [429]

    async def test_un_solo_bot_sin_canal_en_las_claves_funciona(self, entorno):
        e = entorno
        ana = await _abrir(e, "fija-0")
        r = await _mensaje(e, ana)
        assert r.status_code == 200
        assert _eventos(r)[-1] == {"type": "done", "content": "ok", "attachments": []}

    async def test_usuario_limitado_puede_crear_cinco_sesiones_por_minuto(self, entorno):
        e = entorno
        codigos = [(await _sesion(e, "confianza-b1", remitente="29:u0-b1")).status_code for _ in range(6)]
        assert codigos == [200] * 5 + [429]
        assert (await _sesion(e, "confianza-b1", remitente="29:u1-b1")).status_code == 200
