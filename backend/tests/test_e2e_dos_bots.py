from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_aprobacion_central import _correo
from test_e2e_concurrencia_cron_chat import _chat, _esperar, _llamada, _LLMTemporizado, _maximo_simultaneo
from test_e2e_http_recuperacion import _api, _Instance, _login, _scratch_url

from hermeshq.core.security import hash_password
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatMessage, PublicChatNotification, PublicChatSession
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac
from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, _hash_key

pytestmark = [pytest.mark.integration, requires_database]

CANAL_1, CANAL_2 = "teams-principal", "teams-2"
AAD = {"u1": "aad-u1", "u2": "aad-u2", "u3": "aad-u3"}
EXTERNO = {("u2", 1): "29:u2-b1", ("u2", 2): "29:u2-b2", ("u3", 2): "29:u3-b2"}
CLAVES = {
    "u1-a1-b1": ("a1", "u1", 1, False),
    "u2-a2-b1": ("a2", "u2", 1, False),
    "u2-a2-b2": ("a2", "u2", 2, True),
    "u3-a3-b2": ("a3", "u3", 2, True),
    "u3-a4-b2": ("a4", "u3", 2, True),
}
MEDICIONES: list[dict] = []


def _medicion(**fila) -> None:
    MEDICIONES.append(fila)
    print("MEDICION", json.dumps(fila, ensure_ascii=False, default=str))


async def _crear_mundo(maker, raiz: Path) -> dict:
    m: dict = {"agentes": {}, "usuarios": {}, "claves": {}, "id_clave": {}, "canales": {}}
    async with maker() as s:
        nodo = Node(name="nodo-dos-bots", hostname="localhost")
        s.add(nodo)
        await s.flush()
        for letra in ("a1", "a2", "a3", "a4"):
            espacio = raiz / f"agente-{letra}"
            espacio.mkdir(parents=True)
            agente = Agent(
                node_id=nodo.id,
                name=f"agente-{letra}",
                slug=f"agente-dosbots-{letra}",
                workspace_path=str(espacio),
                status="running",
                model="modelo-simulado",
                enabled_toolsets=[],
                max_iterations=2,
            )
            s.add(agente)
            await s.flush()
            m["agentes"][letra] = agente.id
        for nombre, teams_id in (("u1", "29:u1-b1"), ("u2", "29:u2-b1"), ("u3", None)):
            usuario = User(
                username=f"usuario-{nombre}-{uuid.uuid4().hex[:6]}",
                display_name=f"Usuario {nombre.upper()}",
                password_hash=hash_password("clave-solo-pruebas"),
                role="user",
                teams_id=teams_id,
            )
            s.add(usuario)
            await s.flush()
            m["usuarios"][nombre] = usuario.id
        for usuario, agente in (("u1", "a1"), ("u2", "a2"), ("u3", "a3"), ("u3", "a4")):
            s.add(AgentAssignment(user_id=m["usuarios"][usuario], agent_id=m["agentes"][agente]))
        c1 = BotChannel(slug=CANAL_1, display_name="Teams principal", is_default=True)
        c2 = BotChannel(slug=CANAL_2, display_name="Teams 2", azure_app_id="app-bot-2", tenant_id="tenant-1")
        s.add_all([c1, c2])
        await s.flush()
        m["canales"] = {1: c1.id, 2: c2.id}
        for (usuario, bot), externo in EXTERNO.items():
            s.add(
                UserChannelIdentity(
                    user_id=m["usuarios"][usuario],
                    bot_channel_id=m["canales"][bot],
                    external_user_id=externo,
                    aad_object_id=AAD[usuario],
                    tenant_id="tenant-1",
                    verified_at=datetime.now(UTC),
                )
            )
        for nombre, (agente, usuario, bot, con_canal) in CLAVES.items():
            cruda = f"hqk-{uuid.uuid4().hex}"
            clave = PublicChatApiKey(
                key_hash=_hash_key(cruda),
                key_prefix=cruda[:8],
                label="bridge-teams" if bot == 1 else "bridge-teams-2",
                agent_id=m["agentes"][agente],
                allowed_domains=["*"],
                is_active=True,
                bound_user_id=m["usuarios"][usuario],
                trust_sender_id=False,
                bot_channel_id=m["canales"][bot] if con_canal else None,
            )
            s.add(clave)
            await s.flush()
            m["claves"][nombre] = cruda
            m["id_clave"][nombre] = clave.id
        await s.commit()
    return m


class _Bridge:
    def __init__(self, nombre: str, base: str, canal: str | None, instancia: str, claves: dict[str, str]):
        self.nombre, self.base, self.canal, self.instancia = nombre, base, canal, instancia
        self.claves = claves
        self.recibidas: list[dict] = []
        self.ack = True
        self.respuestas: list[int] = []
        self._ip = 0
        self._activo = False
        self._tarea: asyncio.Task | None = None

    def cabeceras(self, clave: str, *, canal: str | None = "", instancia: str | None = "") -> dict:
        h = {"X-Api-Key": clave}
        canal = self.canal if canal == "" else canal
        instancia = self.instancia if instancia == "" else instancia
        if canal:
            h["X-Bot-Channel"] = canal
        if instancia:
            h["X-Bridge-Instance"] = instancia
        return h

    def _otra_ip(self) -> str:
        self._ip += 1
        return f"10.{len(self.nombre)}.{abs(hash(self.nombre)) % 200}.{self._ip}"

    async def sesion(self, nombre_clave: str, remitente: str, aad: str, **cabeceras) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.base}/api/public/chat/sessions",
                headers={**self.cabeceras(self.claves[nombre_clave], **cabeceras), "X-Real-IP": self._otra_ip()},
                json={"sender_id": remitente, "aad_object_id": aad, "tenant_id": "tenant-1"},
            )
        assert r.status_code == 200, r.text
        return r.json()["session_id"], r.json()["session_token"]

    async def sondear(self, nombre_clave: str, **cabeceras) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15) as c:
            return await c.get(
                f"{self.base}/api/public/chat/notifications",
                headers=self.cabeceras(self.claves[nombre_clave], **cabeceras),
            )

    async def _una_vuelta(self) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            for nombre, clave in list(self.claves.items()):
                r = await c.get(f"{self.base}/api/public/chat/notifications", headers=self.cabeceras(clave))
                self.respuestas.append(r.status_code)
                if r.status_code != 200:
                    continue
                for n in r.json():
                    if any(x["id"] == n["id"] and x["clave"] == nombre for x in self.recibidas):
                        continue
                    self.recibidas.append({**n, "clave": nombre, "bridge": self.nombre, "visto": time.time()})
                    if self.ack:
                        await c.post(
                            f"{self.base}/api/public/chat/notifications/{n['id']}/ack",
                            headers=self.cabeceras(clave),
                        )

    async def _bucle(self) -> None:
        while self._activo:
            try:
                await self._una_vuelta()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)

    def arrancar(self) -> None:
        self._activo = True
        self._tarea = asyncio.create_task(self._bucle())

    async def detener(self) -> None:
        self._activo = False
        while self._tarea is not None and not self._tarea.done():
            self._tarea.cancel()
            await asyncio.wait({self._tarea}, timeout=2)
        if self._tarea is not None and not self._tarea.cancelled():
            self._tarea.exception()

    def de(self, *, schedule_id: str | None = None, task_id: str | None = None) -> list[dict]:
        return [
            n
            for n in self.recibidas
            if (schedule_id is None or n.get("schedule_id") == schedule_id)
            and (task_id is None or n.get("task_id") == task_id)
        ]


@pytest.fixture
async def dos_bots(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    raiz = tmp_path / "workspaces"
    comun = raiz / "comun"
    comun.mkdir(parents=True)
    handler = type("LLMDosBots", (_LLMTemporizado,), {"delay": 0.0, "requests": [], "registros": []})
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{servidor.server_address[1]}/v1"
    mundo = await _crear_mundo(maker, raiz)
    instancias: list[_Instance] = []
    bridges: list[_Bridge] = []

    async def levantar(nombre: str, extra_env: dict | None = None) -> dict:
        inst = _Instance(
            nombre,
            _scratch_url(db_name),
            comun,
            llm_url,
            tmp_path,
            extra_env={"HQ_E2E_WORKSPACE_POR_AGENTE": "1", **(extra_env or {})},
        )
        instancias.append(inst)
        await inst.wait_ready()
        claves = mundo["claves"]
        b1 = _Bridge(
            "bridge-1", inst.base, CANAL_1, "bridge-1-vm", {k: v for k, v in claves.items() if k.endswith("b1")}
        )
        b2 = _Bridge(
            "bridge-2", inst.base, CANAL_2, "bridge-2-vm", {k: v for k, v in claves.items() if k.endswith("b2")}
        )
        bridges.extend([b1, b2])
        return {"inst": inst, "b1": b1, "b2": b2}

    try:
        yield {"maker": maker, "handler": handler, "m": mundo, "levantar": levantar}
    finally:
        for b in bridges:
            await b.detener()
        for inst in instancias:
            inst.kill_group()
        servidor.shutdown()


async def _cron(maker, mundo, agente: str, usuario: str, nombre: str, prompt: str, *, vencido=True, clave=None) -> str:
    proxima = datetime.now(UTC) + (timedelta(seconds=-2) if vencido else timedelta(days=30))
    async with maker() as s:
        fila = ScheduledTask(
            agent_id=mundo["agentes"][agente],
            name=nombre,
            cron_expression="0 0 1 1 *",
            prompt=prompt,
            enabled=True,
            next_run=proxima,
            timezone="UTC",
            owner_user_id=mundo["usuarios"][usuario],
            approval_status="approved",
            notify_public_chat=True,
            delivery_key_id=mundo["id_clave"][clave] if clave else None,
        )
        s.add(fila)
        await s.commit()
        return fila.id


async def _vencer(maker, schedule_id: str) -> None:
    async with maker() as s:
        await s.execute(
            text("UPDATE scheduled_tasks SET next_run = now() - interval '2 seconds' WHERE id = :i"), {"i": schedule_id}
        )
        await s.commit()


async def _corridas(maker, schedule_id: str) -> list[Task]:
    async with maker() as s:
        filas = await s.execute(
            select(Task)
            .where(Task.metadata_json["scheduled_task_id"].as_string() == schedule_id)
            .order_by(Task.queued_at.asc())
        )
        return list(filas.scalars().all())


async def _tarea_con(maker, marca: str) -> Task:
    async with maker() as s:
        filas = (await s.execute(select(Task).where(Task.prompt.contains(marca)).order_by(Task.queued_at))).scalars()
        lista = list(filas.all())
    assert lista, marca
    return lista[-1]


async def _notificaciones(maker, **filtro) -> list[PublicChatNotification]:
    async with maker() as s:
        consulta = select(PublicChatNotification)
        for campo, valor in filtro.items():
            consulta = consulta.where(getattr(PublicChatNotification, campo) == valor)
        return list((await s.execute(consulta.order_by(PublicChatNotification.created_at))).scalars().all())


def _contexto(handler, marca: str) -> str:
    encontradas = [r for r in list(handler.requests) if marca in json.dumps(r, ensure_ascii=False)]
    assert encontradas, f"el modelo nunca recibió {marca}"
    return json.dumps(encontradas[-1], ensure_ascii=False)


def _log(inst: _Instance) -> str:
    inst.log.flush()
    return Path(inst.log.name).read_text(encoding="utf-8", errors="replace")


async def _esperar_entregas(bridges, condicion, timeout: float, descripcion: str):
    async def _ok():
        return condicion()

    return await _esperar(_ok, timeout, descripcion)


async def _demora_entrega(maker, notificacion: dict) -> float | None:
    async with maker() as s:
        tarea = await s.get(Task, notificacion["task_id"])
    if tarea is None or tarea.completed_at is None:
        return None
    return round(notificacion["visto"] - tarea.completed_at.timestamp(), 2)


class TestMensajesEntrantes:
    async def test_cada_bot_llega_al_agente_y_usuario_correctos_con_historial_y_reset_aislados(self, dos_bots):
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-entrantes")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        u1, u2, u3 = (m["usuarios"][x] for x in ("u1", "u2", "u3"))

        s_u1, t_u1 = await b1.sesion("u1-a1-b1", "29:u1-b1", AAD["u1"], canal=None, instancia=None)
        r = await _chat(inst.base, s_u1, t_u1, "Hola MARCA-U1 DEMORA=0.2")
        assert r["type"] == "done" and "MARCA-U1" in r["content"], r
        tarea_u1 = await _tarea_con(maker, "MARCA-U1")
        assert (tarea_u1.agent_id, tarea_u1.metadata_json["thread_user_id"]) == (m["agentes"]["a1"], u1)
        assert tarea_u1.metadata_json["public_chat_api_key_id"] == m["id_clave"]["u1-a1-b1"]
        assert "bot_channel_id" not in tarea_u1.metadata_json, "una clave sin canal no agrega datos nuevos"

        s21, t21 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        s22, t22 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        assert s21 != s22
        async with maker() as s:
            for sid in (s21, s22):
                assert (await s.get(PublicChatSession, sid)).user_id == u2
        uno, dos = await asyncio.gather(
            _chat(inst.base, s21, t21, "Mi dato del bot uno es SECRETO-B1. MARCA-U2B1 DEMORA=0.5"),
            _chat(inst.base, s22, t22, "Mi dato del bot dos es SECRETO-B2. MARCA-U2B2 DEMORA=0.5"),
        )
        assert uno["type"] == dos["type"] == "done", (uno, dos)
        for marca, clave, canal in (("MARCA-U2B1", "u2-a2-b1", None), ("MARCA-U2B2", "u2-a2-b2", m["canales"][2])):
            tarea = await _tarea_con(maker, marca)
            assert tarea.agent_id == m["agentes"]["a2"] and tarea.metadata_json["thread_user_id"] == u2
            assert tarea.metadata_json["public_chat_api_key_id"] == m["id_clave"][clave]
            assert tarea.metadata_json.get("bot_channel_id") == canal

        s3a, t3a = await b2.sesion("u3-a3-b2", EXTERNO[("u3", 2)], AAD["u3"])
        s3b, t3b = await b2.sesion("u3-a4-b2", EXTERNO[("u3", 2)], AAD["u3"])
        tres = await asyncio.gather(
            _chat(inst.base, s3a, t3a, "Consulta MARCA-U3A3 DEMORA=0.3"),
            _chat(inst.base, s3b, t3b, "Consulta MARCA-U3A4 DEMORA=0.3"),
        )
        assert [x["type"] for x in tres] == ["done", "done"], tres
        for marca, agente in (("MARCA-U3A3", "a3"), ("MARCA-U3A4", "a4")):
            tarea = await _tarea_con(maker, marca)
            assert (tarea.agent_id, tarea.metadata_json["thread_user_id"]) == (m["agentes"][agente], u3)

        r1 = await _chat(inst.base, s21, t21, "¿Qué dato te di? PREGUNTA-B1")
        r2 = await _chat(inst.base, s22, t22, "¿Qué dato te di? PREGUNTA-B2")
        assert r1["type"] == r2["type"] == "done"
        ctx_b1, ctx_b2 = _contexto(handler, "PREGUNTA-B1"), _contexto(handler, "PREGUNTA-B2")
        assert "SECRETO-B1" in ctx_b1 and "SECRETO-B2" in ctx_b2, "control: cada bot conserva su propio historial"
        assert "SECRETO-B2" not in ctx_b1 and "SECRETO-B1" not in ctx_b2, "el historial de un bot se filtró al otro"
        assert "MARCA-U3" not in ctx_b1 + ctx_b2 and "MARCA-U1" not in ctx_b1 + ctx_b2

        largo = asyncio.create_task(_chat(inst.base, s22, t22, "Trabajo largo MARCA-LARGOB2 DEMORA=10"))
        await _esperar(lambda: _llamada(handler, "MARCA-LARGOB2"), 30, "chat largo del bot 2 en curso")
        inicio_reset = time.monotonic()
        reinicio = await _chat(inst.base, s21, t21, "/reset")
        _medicion(caso="reset", que="/reset en el bot 1", s=round(time.monotonic() - inicio_reset, 2))
        assert reinicio["type"] == "done" and "reiniciada" in reinicio["content"], reinicio
        assert "Cancelé" not in reinicio["content"], "el /reset del bot 1 canceló la tarea del bot 2"
        assert (await _tarea_con(maker, "MARCA-LARGOB2")).status == "running"
        terminado = await asyncio.wait_for(largo, 60)
        assert terminado["type"] == "done" and "MARCA-LARGOB2" in terminado["content"], terminado
        assert (await _tarea_con(maker, "MARCA-LARGOB2")).status == "completed"

        await _chat(inst.base, s21, t21, "Empecemos PREGUNTA-B1-TRAS-RESET")
        await _chat(inst.base, s22, t22, "Sigo aquí PREGUNTA-B2-TRAS-RESET")
        ctx_b1, ctx_b2 = _contexto(handler, "PREGUNTA-B1-TRAS-RESET"), _contexto(handler, "PREGUNTA-B2-TRAS-RESET")
        assert "SECRETO-B1" not in ctx_b1
        assert "SECRETO-B2" in ctx_b2 or "MARCA-LARGOB2" in ctx_b2, "el /reset del bot 1 vació el historial del bot 2"
        assert (await _tarea_con(maker, "PREGUNTA-B1-TRAS-RESET")).metadata_json["conversation_epoch"] == 1
        assert (await _tarea_con(maker, "PREGUNTA-B2-TRAS-RESET")).metadata_json["conversation_epoch"] == 0

        control = asyncio.create_task(_chat(inst.base, s22, t22, "Otro largo MARCA-CONTROLB2 DEMORA=10"))
        await _esperar(lambda: _llamada(handler, "MARCA-CONTROLB2"), 30, "segundo chat largo del bot 2")
        propio = await _chat(inst.base, s22, t22, "/reset")
        assert "Cancelé 1 tarea" in propio["content"], "control: el /reset del propio bot sí cancela"
        await asyncio.wait_for(control, 60)
        assert (await _tarea_con(maker, "MARCA-CONTROLB2")).status == "cancelled"

    async def test_tres_timeouts_en_el_bot2_no_reinician_el_bot1(self, dos_bots):
        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-racha")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        u2, a2 = m["usuarios"]["u2"], m["agentes"]["a2"]
        s21, t21 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        s22, t22 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        ahora = datetime.now(UTC)
        async with maker() as s:
            for n in range(3):
                s.add(
                    Task(
                        agent_id=a2,
                        prompt=f"pedido que agotó el tiempo {n}",
                        status="failed",
                        error_message="Task exceeded the task timeout",
                        queued_at=ahora - timedelta(minutes=10 - n),
                        started_at=ahora - timedelta(minutes=10 - n),
                        completed_at=ahora - timedelta(minutes=9 - n),
                        metadata_json={
                            "conversation": True,
                            "source": "public_chat",
                            "public_session_id": s22,
                            "thread_user_id": u2,
                            "public_chat_api_key_id": m["id_clave"]["u2-a2-b2"],
                            "bot_channel_id": m["canales"][2],
                        },
                    )
                )
            await s.commit()
        r1 = await _chat(inst.base, s21, t21, "Hola MARCA-RACHAB1 DEMORA=0.2")
        assert r1["type"] == "done"
        assert "auto_reset_aviso" not in (await _tarea_con(maker, "MARCA-RACHAB1")).metadata_json
        r2 = await _chat(inst.base, s22, t22, "Hola MARCA-RACHAB2 DEMORA=0.2")
        assert r2["type"] == "done"
        assert (await _tarea_con(maker, "MARCA-RACHAB2")).metadata_json.get("auto_reset_aviso"), (
            "control: la racha del propio bot sí reinicia"
        )


class TestCronsConcurrentes:
    @pytest.mark.parametrize("modo", ["contrato", "difusion_antigua"])
    async def test_crons_de_varios_usuarios_salen_por_el_bot_correcto(self, dos_bots, modo):
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        entorno = {"HQ_E2E_DIFUSION_ANTIGUA": "1"} if modo == "difusion_antigua" else {}
        pila = await dos_bots["levantar"](f"dosbots-crons-{modo}", entorno)
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        b1.arrancar()
        b2.arrancar()
        await _esperar_entregas(None, lambda: len(b1.respuestas) >= 2 and len(b2.respuestas) >= 3, 20, "sondeos")
        assert set(b1.respuestas) == set(b2.respuestas) == {200}

        headers = await _login(inst.base)
        con_destino = await _cron(
            maker, m, "a2", "u2", "u2-destino-bot2", "Informe DEMORA=3 MARCA-CRONU2B2", vencido=False
        )
        r = await _api(
            "PATCH",
            inst.base,
            f"/scheduled-tasks/{con_destino}",
            headers,
            json={"delivery_key_id": m["id_clave"]["u2-a2-b2"]},
        )
        assert r["delivery_key_id"] == m["id_clave"]["u2-a2-b2"]
        async with httpx.AsyncClient() as c:
            ajena = await c.patch(
                f"{inst.base}/api/scheduled-tasks/{con_destino}",
                headers=headers,
                json={"delivery_key_id": m["id_clave"]["u3-a3-b2"]},
            )
        assert ajena.status_code == 422, "la clave de otro usuario no puede ser destino del cron"
        disparo = time.time()
        ids = {
            "u1": await _cron(maker, m, "a1", "u1", "u1-heredado", "Informe DEMORA=3 MARCA-CRONU1"),
            "u2_sin": await _cron(maker, m, "a2", "u2", "u2-sin-destino", "Informe DEMORA=3 MARCA-CRONU2SIN"),
            "u3_a3": await _cron(maker, m, "a3", "u3", "u3-agente-3", "Informe DEMORA=3 MARCA-CRONU3A3"),
            "u3_a4": await _cron(maker, m, "a4", "u3", "u3-agente-4", "Informe DEMORA=3 MARCA-CRONU3A4"),
        }
        await _vencer(maker, con_destino)
        ids["u2_b2"] = con_destino

        async def _todas():
            listas = {k: await _corridas(maker, v) for k, v in ids.items()}
            ok = all(len(x) == 1 and x[0].status == "completed" for x in listas.values())
            return listas if ok else None

        listas = await _esperar(_todas, 90, "los cinco crons completados")
        esperadas = {"u1": 1, "u2_sin": 2, "u3_a3": 1, "u3_a4": 1, "u2_b2": 2 if modo == "difusion_antigua" else 1}
        await _esperar_entregas(
            None,
            lambda: all(len(b1.de(schedule_id=v)) + len(b2.de(schedule_id=v)) >= esperadas[k] for k, v in ids.items()),
            30,
            "entregas de los crons",
        )
        await asyncio.sleep(3)
        for k, v in ids.items():
            corrida = listas[k][0]
            _medicion(
                caso=f"crons {modo}",
                cron=k,
                disparo_s=round(corrida.queued_at.timestamp() - disparo, 2),
                total_s=round(corrida.completed_at.timestamp() - corrida.queued_at.timestamp(), 2),
                entrega_s=[await _demora_entrega(maker, n) for n in b1.de(schedule_id=v) + b2.de(schedule_id=v)],
            )
        _medicion(caso=f"crons {modo}", llm_simultaneos=_maximo_simultaneo(handler, ("MARCA-CRON",)))
        assert _maximo_simultaneo(handler, ("MARCA-CRON",)) >= 3, "los crons de varios agentes no corrieron a la vez"

        (n_u1,) = b1.de(schedule_id=ids["u1"])
        assert b2.de(schedule_id=ids["u1"]) == []
        assert n_u1["bot_channel_id"] is None and n_u1["recipient_external_id"] is None and n_u1["sender_id"] is None
        assert n_u1["recipient_aad_object_id"] is None and n_u1["origin"] == "cron"
        for k in ("u3_a3", "u3_a4"):
            assert b1.de(schedule_id=ids[k]) == []
            (n,) = b2.de(schedule_id=ids[k])
            assert n["recipient_external_id"] == EXTERNO[("u3", 2)] and n["bot_channel_id"] == m["canales"][2]
        (sin_b1,), (sin_b2,) = b1.de(schedule_id=ids["u2_sin"]), b2.de(schedule_id=ids["u2_sin"])
        assert sin_b1["recipient_external_id"] == EXTERNO[("u2", 1)] and sin_b1["bot_channel_id"] is None
        assert sin_b2["recipient_external_id"] == EXTERNO[("u2", 2)] and sin_b2["bot_channel_id"] == m["canales"][2]
        (en_b2,) = b2.de(schedule_id=ids["u2_b2"])
        assert en_b2["recipient_external_id"] == EXTERNO[("u2", 2)] and "MARCA-CRONU2B2" in en_b2["body"]
        log = _log(inst)
        if modo == "contrato":
            assert b1.de(schedule_id=ids["u2_b2"]) == [], "el cron con destino bot 2 salió también por el bot 1"
            assert f"notif_multiclave_sin_preferencia: cron {ids['u2_sin']}" in log
            assert f"notif_multiclave_sin_preferencia: cron {ids['u2_b2']}" not in log
        else:
            assert len(b1.de(schedule_id=ids["u2_b2"])) == 1, "control: la difusión antigua sale por ambos bots"
        filas = await _notificaciones(maker, schedule_id=ids["u2_b2"])
        assert len(filas) == esperadas["u2_b2"]
        for x in ids:
            assert len(await _corridas(maker, ids[x])) == 1


class TestRespuestaTardia:
    @pytest.mark.parametrize("modo", ["contrato", "difusion_antigua"])
    async def test_respuesta_tardia_de_un_chat_del_bot2_vuelve_solo_por_el_bot2(self, dos_bots, modo):
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        entorno = {"HQ_E2E_DIFUSION_ANTIGUA": "1"} if modo == "difusion_antigua" else {}
        pila = await dos_bots["levantar"](f"dosbots-tardia-{modo}", entorno)
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        b1.arrancar()
        b2.arrancar()
        await _esperar_entregas(None, lambda: len(b1.respuestas) >= 2 and len(b2.respuestas) >= 3, 20, "sondeos")
        s22, t22 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        chat = asyncio.create_task(_chat(inst.base, s22, t22, "Informe pesado MARCA-TARDIA DEMORA=8"))
        await _esperar(lambda: _llamada(handler, "MARCA-TARDIA"), 30, "chat del bot 2 en curso")
        tarea = await _tarea_con(maker, "MARCA-TARDIA")
        async with maker() as s:
            await s.execute(
                text(
                    "UPDATE tasks SET metadata = (metadata::jsonb || '{\"respuesta_tardia\": true}'::jsonb)::json "
                    "WHERE id = :i"
                ),
                {"i": tarea.id},
            )
            await s.commit()
        chat.cancel()
        await _esperar_entregas(None, lambda: b2.de(task_id=tarea.id), 40, "respuesta tardía en el bot 2")
        await asyncio.sleep(3)
        (n,) = b2.de(task_id=tarea.id)
        assert n["origin"] == "respuesta_tardia" and "MARCA-TARDIA" in n["body"]
        assert n["recipient_external_id"] == EXTERNO[("u2", 2)] and n["bot_channel_id"] == m["canales"][2]
        _medicion(caso=f"respuesta tardia {modo}", entrega_s=await _demora_entrega(maker, n))
        if modo == "contrato":
            assert b1.de(task_id=tarea.id) == [], "la respuesta tardía del bot 2 salió también por el bot 1"
        else:
            assert len(b1.de(task_id=tarea.id)) == 1, "control: la difusión antigua la entrega en ambos bots"


async def _decidir(
    inst, clave: str, canal: str | None, aprobacion, sesion: tuple[str, str], aad: str, decision="rechazar"
):
    cabeceras = {"X-Api-Key": clave, **({"X-Bot-Channel": canal} if canal else {})}
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.post(
            f"{inst.base}/api/public/chat/aprobaciones/{aprobacion.id}/decision",
            headers=cabeceras,
            json={
                "session_id": sesion[0],
                "session_token": sesion[1],
                "decision": decision,
                "huella": aprobacion.huella,
                "aad_object_id": aad,
                "tenant_id": "tenant-1",
            },
        )


def _codigo(r: httpx.Response) -> str | None:
    detalle = r.json().get("detail")
    return detalle.get("code") or detalle.get("codigo") if isinstance(detalle, dict) else None


class TestAprobaciones:
    async def test_aprobacion_del_bot2_no_se_decide_desde_el_bot1_ni_con_otro_aad(self, dos_bots):
        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-aprobaciones")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        u2, a2 = m["usuarios"]["u2"], m["agentes"]["a2"]
        k1, k2 = m["claves"]["u2-a2-b1"], m["claves"]["u2-a2-b2"]
        sesion_b1 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        sesion_b2 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        r = await _chat(inst.base, *sesion_b2, "Prepara el correo MARCA-APROB DEMORA=0.2")
        assert r["type"] == "done"
        origen = await _tarea_con(maker, "MARCA-APROB")
        async with maker() as s:
            ctx = ac.Contexto(
                agent_id=a2, user_id=u2, conversacion_id=sesion_b2[0], task_id=origen.id, canal="public_chat"
            )
            aprobacion = await ac.crear(s, ac.TIPO_CORREO, _correo(), ctx)

        async def _pendientes(bridge, clave, sesion):
            async with httpx.AsyncClient(timeout=15) as c:
                return await c.get(
                    f"{inst.base}/api/public/chat/aprobaciones/pendientes",
                    headers=bridge.cabeceras(clave),
                    params={"session_id": sesion[0], "session_token": sesion[1]},
                )

        en_b1, en_b2 = await _pendientes(b1, k1, sesion_b1), await _pendientes(b2, k2, sesion_b2)
        assert en_b1.json()["aprobaciones"] == [], "la tarjeta del bot 2 aparece en el bot 1"
        assert [a["id"] for a in en_b2.json()["aprobaciones"]] == [aprobacion.id]

        intentos = {
            "sesion del bot 1": (
                await _decidir(inst, k1, CANAL_1, aprobacion, sesion_b1, AAD["u2"]),
                409,
                "otra_conversacion",
            ),
            "clave bot 1, sesion bot 2": (
                await _decidir(inst, k1, CANAL_1, aprobacion, sesion_b2, AAD["u2"]),
                403,
                "otra_conversacion",
            ),
            "clave bot 2 por el canal 1": (
                await _decidir(inst, k2, CANAL_1, aprobacion, sesion_b2, AAD["u2"]),
                403,
                "canal_incorrecto",
            ),
            "otro AAD": (await _decidir(inst, k2, CANAL_2, aprobacion, sesion_b2, AAD["u3"]), 403, "otro_usuario"),
        }
        for nombre, (respuesta, estado, codigo) in intentos.items():
            assert (respuesta.status_code, _codigo(respuesta)) == (estado, codigo), (nombre, respuesta.text)
        async with maker() as s:
            assert (await ac._cargar(s, aprobacion.id)).estado == "pendiente"
        valida = await _decidir(inst, k2, CANAL_2, aprobacion, sesion_b2, AAD["u2"])
        assert valida.status_code == 200 and valida.json()["estado"] == "rechazada", "control: el bot 2 sí decide"


class TestCabeceras:
    async def test_canal_incorrecto_y_clave_compartida(self, dos_bots):
        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-cabeceras")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        equivocado = await b2.sondear("u3-a3-b2", canal=CANAL_1)
        assert (equivocado.status_code, _codigo(equivocado)) == (403, "canal_incorrecto")
        assert (await b2.sondear("u3-a3-b2")).status_code == 200, "control: con su canal se acepta"
        assert (await b2.sondear("u3-a3-b2", canal=None)).status_code == 200, "sin cabecera se acepta (no exigida)"
        async with httpx.AsyncClient(timeout=15) as c:
            sesion = await c.post(
                f"{inst.base}/api/public/chat/sessions",
                headers={**b1.cabeceras(m["claves"]["u2-a2-b1"], canal=CANAL_2), "X-Real-IP": "10.9.9.1"},
                json={"sender_id": EXTERNO[("u2", 1)], "aad_object_id": AAD["u2"]},
            )
        assert (sesion.status_code, _codigo(sesion)) == (403, "canal_incorrecto")
        assert (await b1.sondear("u1-a1-b1", canal=None, instancia=None)).status_code == 200
        assert (await b1.sondear("u1-a1-b1")).status_code == 200, "una clave sin canal es del canal por defecto"

        for _ in range(2):
            assert (await b2.sondear("u3-a4-b2")).status_code == 200
        assert "clave_compartida" not in _log(inst), "control: la misma instancia no es una clave compartida"
        otro = await b2.sondear("u3-a4-b2", instancia="bridge-2-copia")
        assert otro.status_code == 200
        await asyncio.sleep(0.5)
        assert f"clave_compartida: la clave {m['id_clave']['u3-a4-b2']}" in _log(inst)
        async with maker() as s:
            await s.execute(
                text("UPDATE bot_channels SET require_channel_header = true WHERE slug = :s"), {"s": CANAL_2}
            )
            await s.commit()
        rechazo = await b2.sondear("u3-a4-b2")
        assert (rechazo.status_code, _codigo(rechazo)) == (409, "clave_compartida")
        sin_cabecera = await b2.sondear("u3-a3-b2", canal=None)
        assert (sin_cabecera.status_code, _codigo(sin_cabecera)) == (403, "canal_incorrecto")


class TestReasignacion:
    async def test_desactivar_la_clave_del_bot1_cae_al_respaldo_sin_perder_datos_ni_exponerlos(self, dos_bots):
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-reasignacion")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        u2, u3, a2 = m["usuarios"]["u2"], m["usuarios"]["u3"], m["agentes"]["a2"]
        id_k1 = m["id_clave"]["u2-a2-b1"]
        b1.ack = False
        b1.arrancar()
        b2.arrancar()
        await _esperar_entregas(None, lambda: len(b1.respuestas) >= 2 and len(b2.respuestas) >= 3, 20, "sondeos")
        headers = await _login(inst.base)

        s21, t21 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        s21b, t21b = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        r = await _chat(inst.base, s21, t21, "Mi dato privado es SECRETO-U2-B1. MARCA-REASIG DEMORA=0.2")
        assert r["type"] == "done"
        r = await _chat(inst.base, s21b, t21b, "Recuerda SECRETO-U2-B1B. MARCA-REASIGB DEMORA=0.2")
        assert r["type"] == "done"

        cron = await _cron(maker, m, "a2", "u2", "u2-por-bot1", "Informe DEMORA=1 MARCA-CRONREASIG", vencido=False)
        await _api("PATCH", inst.base, f"/scheduled-tasks/{cron}", headers, json={"delivery_key_id": id_k1})
        await _vencer(maker, cron)
        await _esperar_entregas(None, lambda: b1.de(schedule_id=cron), 60, "cron por la clave del bot 1")
        await asyncio.sleep(3)
        assert len(b1.de(schedule_id=cron)) == 1 and b2.de(schedule_id=cron) == [], "control: sale solo por el bot 1"
        primera = b1.de(schedule_id=cron)[0]

        await _api("DELETE", inst.base, f"/settings/public-chat-keys/{id_k1}", headers)
        assert (await b1.sondear("u2-a2-b1")).status_code == 401
        await _vencer(maker, cron)
        await _esperar_entregas(None, lambda: b2.de(schedule_id=cron), 60, "cron por la regla de respaldo")
        await asyncio.sleep(3)
        (respaldo,) = b2.de(schedule_id=cron)
        assert respaldo["recipient_external_id"] == EXTERNO[("u2", 2)]
        assert len(b1.de(schedule_id=cron)) == 1
        assert f"Cron {cron}: delivery_key_id {id_k1} no es válida" in _log(inst)
        _medicion(caso="reasignacion", entrega_respaldo_s=await _demora_entrega(maker, respaldo))

        async with maker() as s:
            fila = await s.get(ScheduledTask, cron)
            assert (fila.enabled, fila.owner_user_id, fila.delivery_key_id) == (True, u2, id_k1)
            mensajes = (
                (await s.execute(select(PublicChatMessage).where(PublicChatMessage.session_id == s21))).scalars().all()
            )
            assert any("SECRETO-U2-B1" in (x.content or "") for x in mensajes), "U2 perdió su historial del bot 1"
            pendiente = await s.get(PublicChatNotification, primera["id"])
            assert (pendiente.status, pendiente.recipient_user_id, pendiente.api_key_id) == ("pending", u2, id_k1)

        async with httpx.AsyncClient(timeout=30) as c:
            inactiva = await c.post(
                f"{inst.base}/api/public/chat/sessions/{s21b}/messages",
                headers={"X-Session-Token": t21b},
                json={"content": "Sigo por aquí MARCA-CLAVEINACTIVA DEMORA=0.2"},
            )
        assert inactiva.status_code == 400, "una clave desactivada siguió atendiendo una sesión abierta"
        assert not [t for t in handler.requests if "MARCA-CLAVEINACTIVA" in json.dumps(t, ensure_ascii=False)]

        async with maker() as s:
            s.add(AgentAssignment(user_id=u3, agent_id=a2))
            await s.commit()
        await _api(
            "PATCH",
            inst.base,
            f"/settings/public-chat-keys/{id_k1}",
            headers,
            json={"is_active": True, "bound_user_id": u3},
        )
        vistas = await b1.sondear("u2-a2-b1")
        assert vistas.status_code == 200 and vistas.json() == [], "R10: la clave reasignada expone avisos de U2"
        async with maker() as s:
            sigue = await s.get(PublicChatNotification, primera["id"])
        assert sigue.status == "pending" and sigue.recipient_user_id == u2, "control: el aviso de U2 sigue ahí"

        async with httpx.AsyncClient(timeout=30) as c:
            heredada = await c.post(
                f"{inst.base}/api/public/chat/sessions/{s21}/messages",
                headers={"X-Session-Token": t21},
                json={"content": "¿Qué dato privado conoces? MARCA-REASIGNADA DEMORA=0.2"},
            )
        llegaron = [t for t in handler.requests if "MARCA-REASIGNADA" in json.dumps(t, ensure_ascii=False)]
        assert not any("SECRETO-U2-B1" in json.dumps(t, ensure_ascii=False) for t in llegaron), (
            "la sesión de U2 siguió viva con la clave reasignada y U3 recibió su historial"
        )
        assert heredada.status_code == 400
        nueva = await b1.sesion("u2-a2-b1", "29:u3-b1", AAD["u3"])
        assert nueva[0] not in (s21, s21b)
        r = await _chat(inst.base, *nueva, "Hola MARCA-U3NUEVA DEMORA=0.2")
        assert r["type"] == "done"
        tarea = await _tarea_con(maker, "MARCA-U3NUEVA")
        assert tarea.metadata_json["thread_user_id"] == u3
        assert "SECRETO-U2-B1" not in _contexto(handler, "MARCA-U3NUEVA")


def _ruta_bridge() -> Path | None:
    for candidata in (os.environ.get("HQ_BRIDGE_ARCHIVO"), "/bridge/hermes-teams-bridge.py"):
        if candidata and Path(candidata).is_file():
            return Path(candidata)
    return None


def _cargar_bridge(ruta: Path):
    if str(ruta.parent) not in sys.path:
        sys.path.insert(0, str(ruta.parent))
    spec = importlib.util.spec_from_file_location(f"bridge_dosbots_{uuid.uuid4().hex[:6]}", str(ruta))
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    modulo.NOTIF_INTERVALO = 1
    return modulo


class _BotFalso:
    def __init__(self, conversaciones: dict[str, list[dict]]):
        from aiohttp import web

        self.conversaciones = conversaciones
        self.publicadas: list[dict] = []
        self.app = web.Application()
        self.app.router.add_get("/api/conversations", self._convs)
        self.app.router.add_post("/api/proactive", self._proactive)

    async def _convs(self, req):
        from aiohttp import web

        return web.json_response(self.conversaciones.get(req.headers.get("X-Hermes-Token"), []))

    async def _proactive(self, req):
        from aiohttp import web

        self.publicadas.append({"token": req.headers.get("X-Hermes-Token"), **(await req.json()), "t": time.time()})
        return web.Response(status=200)

    async def arrancar(self):
        from aiohttp import web

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        sitio = web.TCPSite(self.runner, "127.0.0.1", 0)
        await sitio.start()
        self.url = f"http://127.0.0.1:{sitio._server.sockets[0].getsockname()[1]}"

    async def cerrar(self):
        await self.runner.cleanup()

    def en(self, conversacion: str, marca: str) -> list[dict]:
        return [p for p in self.publicadas if p["conversation_id"] == conversacion and marca in json.dumps(p)]


class TestBridgeReal:
    async def test_el_bridge_real_entrega_cada_cron_en_la_conversacion_del_destinatario(self, dos_bots):
        ruta = _ruta_bridge()
        if ruta is None:
            pytest.skip("sin copia del bridge (HQ_BRIDGE_ARCHIVO o /bridge)")
        import aiohttp

        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("dosbots-bridge-real")
        inst = pila["inst"]
        conv = {
            "u2-b1": {"conversation_id": "conv-u2-b1", "user_id": EXTERNO[("u2", 1)], "aad_object_id": AAD["u2"]},
            "x-b1": {"conversation_id": "conv-ajeno-b1", "user_id": "29:ajeno-b1", "aad_object_id": "aad-ajeno"},
            "u2-b2": {"conversation_id": "conv-u2-b2", "user_id": EXTERNO[("u2", 2)], "aad_object_id": AAD["u2"]},
            "x-b2": {"conversation_id": "conv-ajeno-b2", "user_id": "29:ajeno-b2", "aad_object_id": "aad-ajeno"},
            "u3-b2": {"conversation_id": "conv-u3-b2", "user_id": EXTERNO[("u3", 2)], "aad_object_id": AAD["u3"]},
        }
        bot1 = _BotFalso({"token-bot-1": [conv["x-b1"], conv["u2-b1"]]})
        bot2 = _BotFalso({"token-bot-2": [conv["x-b2"], conv["u2-b2"]], "token-bot-2-u3": [conv["u3-b2"]]})
        await bot1.arrancar()
        await bot2.arrancar()
        puente1, puente2 = _cargar_bridge(ruta), _cargar_bridge(ruta)
        cfg1 = {
            "hermeshq_url": inst.base,
            "bot_url": bot1.url,
            "adjuntos": False,
            "bot_channel": CANAL_1,
            "instancia_bridge": "bridge-real-1",
        }
        cfg2 = {
            "hermeshq_url": inst.base,
            "bot_url": bot2.url,
            "adjuntos": False,
            "bot_channel": CANAL_2,
            "instancia_bridge": "bridge-real-2",
        }
        agentes1 = [{"hermes_id": "a2-b1", "public_chat_key": m["claves"]["u2-a2-b1"], "bot_token": "token-bot-1"}]
        agentes2 = [
            {"hermes_id": "a2-b2", "public_chat_key": m["claves"]["u2-a2-b2"], "bot_token": "token-bot-2"},
            {"hermes_id": "a3-b2", "public_chat_key": m["claves"]["u3-a3-b2"], "bot_token": "token-bot-2-u3"},
        ]
        tareas = []
        async with aiohttp.ClientSession() as sesion:
            try:
                for puente, cfg, agentes in ((puente1, cfg1, agentes1), (puente2, cfg2, agentes2)):
                    for agente in agentes:
                        tareas.append(asyncio.create_task(puente.bucle_notificaciones(agente, cfg, sesion)))
                await asyncio.sleep(3)
                ids = {
                    "destino": await _cron(
                        maker, m, "a2", "u2", "bridge-destino", "Informe DEMORA=1 MARCA-BRDEST", clave="u2-a2-b2"
                    ),
                    "difusion": await _cron(maker, m, "a2", "u2", "bridge-difusion", "Informe DEMORA=1 MARCA-BRDIF"),
                    "u3": await _cron(maker, m, "a3", "u3", "bridge-u3", "Informe DEMORA=1 MARCA-BRU3"),
                }

                def _listo():
                    return (
                        bot2.en("conv-u2-b2", "MARCA-BRDEST")
                        and bot2.en("conv-u2-b2", "MARCA-BRDIF")
                        and bot1.en("conv-u2-b1", "MARCA-BRDIF")
                        and bot2.en("conv-u3-b2", "MARCA-BRU3")
                    )

                await _esperar_entregas(None, _listo, 90, "publicación del bridge real en Teams simulado")
                await asyncio.sleep(3)
            finally:
                for t in tareas:
                    t.cancel()
                await asyncio.gather(*tareas, return_exceptions=True)
                await bot1.cerrar()
                await bot2.cerrar()
        assert bot1.en("conv-u2-b1", "MARCA-BRDEST") == [], "el cron con destino bot 2 llegó al bot 1"
        assert bot1.en("conv-ajeno-b1", "MARCA") == [] and bot2.en("conv-ajeno-b2", "MARCA") == []
        for conversacion, marca in (
            ("conv-u2-b2", "MARCA-BRDEST"),
            ("conv-u2-b2", "MARCA-BRDIF"),
            ("conv-u2-b1", "MARCA-BRDIF"),
            ("conv-u3-b2", "MARCA-BRU3"),
        ):
            bot = bot1 if conversacion.endswith("b1") else bot2
            assert len(bot.en(conversacion, marca)) == 1, (conversacion, marca)
        for clave, valor in ids.items():
            corrida = (await _corridas(maker, valor))[0]
            publicadas = [p for p in bot1.publicadas + bot2.publicadas if corrida.title in json.dumps(p)]
            _medicion(
                caso="bridge real",
                cron=clave,
                entrega_teams_s=[round(p["t"] - corrida.completed_at.timestamp(), 2) for p in publicadas],
            )
        for n in await _notificaciones(maker, schedule_id=ids["difusion"]):
            assert n.status == "delivered", "el bridge real no confirmó la entrega"
        assert puente2.elegir_conversacion([conv["x-b2"], conv["u2-b2"]], None) is None, (
            "control: sin recipient_external_id el bridge no sabe a quién entregar en un bot con varios usuarios"
        )
        assert puente2.elegir_conversacion([conv["x-b2"], conv["u2-b2"]], None, EXTERNO[("u2", 2)]) == "conv-u2-b2"
        assert puente2.elegir_conversacion([conv["x-b2"]], None, EXTERNO[("u2", 1)], "aad-otro") is None


class TestSesionAjena:
    def _clave(self, activa=True):
        return PublicChatApiKey(key_hash="h", key_prefix="p", label="bridge-teams", agent_id="a", is_active=activa)

    def test_clave_activa_y_mismo_usuario_sigue(self):
        from hermeshq.services.public_chat_service import PublicChatService

        sesion = PublicChatSession(api_key_id="k", agent_id="a", session_token="t", user_id="u2")
        assert not PublicChatService.sesion_ajena(self._clave(), sesion, "u2")
        heredada = PublicChatSession(api_key_id="k", agent_id="a", session_token="t", user_id=None)
        assert not PublicChatService.sesion_ajena(self._clave(), heredada, PUBLIC_CHAT_USER_ID)

    def test_clave_inactiva_o_reasignada_corta(self):
        from hermeshq.services.public_chat_service import PublicChatService

        sesion = PublicChatSession(api_key_id="k", agent_id="a", session_token="t", user_id="u2")
        assert PublicChatService.sesion_ajena(self._clave(activa=False), sesion, PUBLIC_CHAT_USER_ID)
        assert PublicChatService.sesion_ajena(self._clave(), sesion, "u3")
        assert PublicChatService.sesion_ajena(self._clave(), sesion, PUBLIC_CHAT_USER_ID)
