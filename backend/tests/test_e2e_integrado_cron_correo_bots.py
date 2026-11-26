from __future__ import annotations

import asyncio
import importlib.util
import json
import re
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
from test_e2e_concurrencia_cron_chat import _esperar
from test_e2e_dos_bots import CANAL_1, CANAL_2, _Bridge, _codigo, _corridas, _medicion, _vencer
from test_e2e_http_recuperacion import _Instance, _scratch_url
from test_e2e_segundo_correo import (
    MARCA_CRON,
    POLITICA,
    TEXTO_RESPUESTA,
    TOKEN_SIMULADO,
    _correo,
    _GraphBandeja,
    _mid,
    _ModeloQueLlamaHerramientas,
    _texto,
)

from hermeshq.core.security import hash_password
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.public_chat_service import _hash_key

pytestmark = [pytest.mark.integration, requires_database]

AAD = {"u1": "aad-u1", "u2": "aad-u2", "u3": "aad-u3"}
EXTERNO = {("u2", 1): "29:u2-b1", ("u2", 2): "29:u2-b2", ("u3", 2): "29:u3-b2"}
MODELO = {"a1": "modelo-a1", "a2": "modelo-a2", "a3": "modelo-a3"}
CLAVES = {
    "u1-a1-b1": ("a1", "u1", 1, False),
    "u2-a2-b1": ("a2", "u2", 1, False),
    "u2-a2-b2": ("a2", "u2", 2, True),
    "u3-a3-b2": ("a3", "u3", 2, True),
}
PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)


class _ModeloIntegrado(_ModeloQueLlamaHerramientas):
    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(largo) or b"{}")
        type(self).requests.append(pedido)
        mensajes = pedido.get("messages") or []
        ultimo = max((i for i, m in enumerate(mensajes) if m.get("role") == "user"), default=-1)
        usuario = str(mensajes[ultimo].get("content") or "") if ultimo >= 0 else ""
        resultados = [m for m in mensajes[ultimo + 1 :] if m.get("role") == "tool"]
        paso = len(resultados)
        previo = {}
        if resultados:
            try:
                previo = json.loads(resultados[-1].get("content") or "{}")
            except ValueError:
                previo = {"crudo": resultados[-1].get("content")}
        if pedido.get("model") != MODELO["a2"]:
            chunks = _texto("ECO " + " ".join(re.findall(r"MARCA-[A-Z0-9-]+", usuario)))
        elif MARCA_CRON in usuario:
            chunks = self._cron(paso, previo)
        else:
            chunks = self._chat(usuario, paso, previo)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


async def _crear_mundo(maker, raiz: Path) -> dict:
    m: dict = {"agentes": {}, "usuarios": {}, "claves": {}, "id_clave": {}, "canales": {}, "espacios": {}}
    async with maker() as s:
        ajustes = await s.get(AppSettings, "default")
        if ajustes is None:
            ajustes = AppSettings(id="default")
            s.add(ajustes)
        ajustes.enabled_integration_packages = ["ms365-mail"]
        nodo = Node(name="nodo-integrado", hostname="localhost")
        s.add(nodo)
        await s.flush()
        for letra in ("a1", "a2", "a3"):
            espacio = raiz / f"agente-{letra}"
            (espacio / "work").mkdir(parents=True)
            correo = letra == "a2"
            if correo:
                (espacio / "work" / "correo_unificado_estado.json").write_text(
                    json.dumps({"next_display_id": 849}), encoding="utf-8"
                )
            agente = Agent(
                node_id=nodo.id,
                name=f"agente-integrado-{letra}",
                slug=f"agente-integrado-{letra}",
                workspace_path=str(espacio),
                status="running",
                model=MODELO[letra],
                enabled_toolsets=["hermeshq_ms365_mail"] if correo else [],
                integration_configs={"ms365-mail": {}, "m365_graph": POLITICA} if correo else {},
                max_iterations=8,
                thinking_mode="off",
            )
            s.add(agente)
            await s.flush()
            m["agentes"][letra] = agente.id
            m["espacios"][letra] = espacio
        for nombre, teams_id in (("u1", "29:u1-b1"), ("u2", "29:u2-b1"), ("u3", None)):
            usuario = User(
                username=f"integrado-{nombre}-{uuid.uuid4().hex[:6]}",
                display_name=f"Usuario {nombre.upper()}",
                password_hash=hash_password("clave-solo-pruebas"),
                role="user",
                teams_id=teams_id,
            )
            s.add(usuario)
            await s.flush()
            m["usuarios"][nombre] = usuario.id
        for usuario, agente in (("u1", "a1"), ("u2", "a2"), ("u3", "a3")):
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
        crons = {}
        for clave_cron, agente, usuario, prompt, destino in (
            ("u2", "a2", "u2", f"{MARCA_CRON}: arma el informe de correos con ms365_correo_sync", "u2-a2-b2"),
            ("u1", "a1", "u1", "Informe diario MARCA-CRONU1", None),
            ("u3", "a3", "u3", "Informe diario MARCA-CRONU3", None),
        ):
            fila = ScheduledTask(
                agent_id=m["agentes"][agente],
                name=f"Informe {clave_cron}",
                cron_expression="0 3 * * *",
                prompt=prompt,
                enabled=True,
                timezone="America/Santiago",
                owner_user_id=m["usuarios"][usuario],
                approval_status="approved",
                notify_public_chat=True,
                next_run=datetime.now(UTC) + timedelta(days=1),
                delivery_key_id=m["id_clave"][destino] if destino else None,
            )
            s.add(fila)
            await s.flush()
            crons[clave_cron] = fila.id
        m["crons"] = crons
        await s.commit()
    return m


class _Escenario:
    def __init__(self, maker, db_name, tmp_path):
        self.maker, self.db_name, self.tmp = maker, db_name, tmp_path
        self.raiz = tmp_path / "workspaces"
        self.comun = self.raiz / "comun"
        self.comun.mkdir(parents=True)
        self.graph = type(
            "GraphIntegrado",
            (_GraphBandeja,),
            {"bandeja": {}, "borradores": {}, "marcados": [], "enviados": [], "peticiones": [], "modo": "202"},
        )
        self.servidor_graph = ThreadingHTTPServer(("127.0.0.1", 0), self.graph)
        threading.Thread(target=self.servidor_graph.serve_forever, daemon=True).start()
        self.llm = type(
            "LLMIntegrado",
            (_ModeloIntegrado,),
            {"delay": 0.0, "requests": [], "categorias": {}, "ultima_respuesta": {}},
        )
        self.servidor_llm = ThreadingHTTPServer(("127.0.0.1", 0), self.llm)
        threading.Thread(target=self.servidor_llm.serve_forever, daemon=True).start()
        self.inst: _Instance | None = None
        self.bridges: list[_Bridge] = []

    async def levantar(self, nombre: str) -> None:
        self.inst = _Instance(
            nombre,
            _scratch_url(self.db_name),
            self.comun,
            f"http://127.0.0.1:{self.servidor_llm.server_address[1]}/v1",
            self.tmp,
            extra_env={
                "HQ_E2E_WORKSPACE_POR_AGENTE": "1",
                "HQ_E2E_INSTALACION_REAL": "1",
                "APPROVAL_CENTRAL_ENABLED": "true",
                "M365_AGENT_TOKEN_ENABLED": "false",
                "APPROVAL_RECONCILE_WAIT_SECONDS": "0",
                "HERMESHQ_ENTORNO": "aislado",
                "HERMESHQ_GRAPH_BASE_SIMULADO": f"http://127.0.0.1:{self.servidor_graph.server_address[1]}/v1",
                "HERMESHQ_GRAPH_TOKEN_SIMULADO": TOKEN_SIMULADO,
            },
        )
        await self.inst.wait_ready()
        self.base = self.inst.base
        for b in self.bridges:
            b.base = self.base

    async def reiniciar(self) -> float:
        inicio = time.monotonic()
        self.inst.kill_group()
        await self.levantar(f"backend-integrado-{uuid.uuid4().hex[:4]}")
        await self.vueltas_completas(2)
        return round(time.monotonic() - inicio, 2)

    async def arrancar(self) -> None:
        self.m = await _crear_mundo(self.maker, self.raiz)
        await self.levantar("backend-integrado")
        claves = self.m["claves"]
        self.b1 = _Bridge(
            "bridge-1", self.base, CANAL_1, "bridge-1-vm", {k: v for k, v in claves.items() if k.endswith("b1")}
        )
        self.b2 = _Bridge(
            "bridge-2", self.base, CANAL_2, "bridge-2-vm", {k: v for k, v in claves.items() if k.endswith("b2")}
        )
        self.bridges = [self.b1, self.b2]
        for b in self.bridges:
            b.arrancar()

        async def _sondeando():
            return len(self.b1.respuestas) >= 4 and len(self.b2.respuestas) >= 4

        await _esperar(_sondeando, 30, "los dos bridges sondean")
        assert set(self.b1.respuestas) == set(self.b2.respuestas) == {200}

    def agregar(self, *correos) -> None:
        for correo in correos:
            self.graph.bandeja[correo["id"]] = correo
            self.llm.categorias[correo["subject"]] = correo["_categoria"]

    async def disparar(self, *claves: str) -> dict[str, Task]:
        previas = {c: {t.id for t in await _corridas(self.maker, self.m["crons"][c])} for c in claves}
        for c in claves:
            await _vencer(self.maker, self.m["crons"][c])

        async def _listas():
            salida = {}
            for c in claves:
                nuevas = [t for t in await _corridas(self.maker, self.m["crons"][c]) if t.id not in previas[c]]
                if len(nuevas) != 1 or nuevas[0].status not in ("completed", "failed", "cancelled"):
                    return None
                salida[c] = nuevas[0]
            return salida

        corridas = await _esperar(_listas, 180, f"crons {claves} terminados")
        for c, t in corridas.items():
            assert t.status == "completed", (c, t.status, t.error_message)
        return corridas

    async def entregado(self, bridge: _Bridge, task_id: str) -> dict:
        async def _visto():
            return bridge.de(task_id=task_id)

        [aviso] = await _esperar(_visto, 60, f"aviso de {task_id} en {bridge.nombre}")
        return aviso

    async def entregado_en_bd(self, task_id: str) -> None:
        async def _confirmado():
            async with self.maker() as s:
                filas = (
                    (await s.execute(select(PublicChatNotification).where(PublicChatNotification.task_id == task_id)))
                    .scalars()
                    .all()
                )
            return filas and all(f.status == "delivered" for f in filas)

        await _esperar(_confirmado, 30, f"aviso de {task_id} confirmado")

    async def vueltas_completas(self, n: int = 2) -> None:
        objetivo = {b.nombre: len(b.respuestas) + n * len(b.claves) for b in self.bridges}

        async def _hecho():
            return all(len(b.respuestas) >= objetivo[b.nombre] for b in self.bridges)

        await _esperar(_hecho, 30, "vueltas de sondeo")

    async def decir(self, sesion: tuple[str, str], contenido: str) -> dict:
        inicio = time.monotonic()
        async with (
            httpx.AsyncClient(timeout=240) as c,
            c.stream(
                "POST",
                f"{self.base}/api/public/chat/sessions/{sesion[0]}/messages",
                headers={"X-Session-Token": sesion[1]},
                json={"content": contenido},
            ) as r,
        ):
            assert r.status_code == 200, await r.aread()
            async for linea in r.aiter_lines():
                if linea.startswith("data: "):
                    evento = json.loads(linea[6:])
                    if evento.get("type") in ("done", "error", "cancelled"):
                        evento["duracion_s"] = round(time.monotonic() - inicio, 2)
                        return evento
        raise AssertionError("el chat no terminó")

    async def pendientes(self, bridge: _Bridge, nombre_clave: str, sesion) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{self.base}/api/public/chat/aprobaciones/pendientes",
                headers=bridge.cabeceras(bridge.claves[nombre_clave]),
                params={"session_id": sesion[0], "session_token": sesion[1]},
            )
        assert r.status_code == 200, r.text
        return r.json()["aprobaciones"]

    async def decidir(self, bridge: _Bridge, nombre_clave: str, sesion, aprob: dict, aad: str, decision="aprobar"):
        async with httpx.AsyncClient(timeout=30) as c:
            return await c.post(
                f"{self.base}/api/public/chat/aprobaciones/{aprob['id']}/decision",
                headers=bridge.cabeceras(bridge.claves[nombre_clave]),
                json={
                    "session_id": sesion[0],
                    "session_token": sesion[1],
                    "decision": decision,
                    "huella": aprob["huella"],
                    "aad_object_id": aad,
                    "tenant_id": "tenant-1",
                },
            )

    async def aprobaciones(self) -> list[dict]:
        async with self.maker() as s:
            filas = await s.execute(
                text(
                    "SELECT id, estado, tipo_accion, agent_id, propietario_user_id, conversacion_id, contenido_json, "
                    "envio_estado FROM approvals ORDER BY creado_en"
                )
            )
            return [dict(f) for f in filas.mappings().all()]

    def peticiones_graph(self, sufijo: str) -> list[dict]:
        return [p for p in list(self.graph.peticiones) if p["ruta"].split("?")[0].endswith(sufijo)]

    def envios(self) -> int:
        return len(self.peticiones_graph("/send"))

    def cerrar(self) -> None:
        if self.inst:
            self.inst.kill_group()
        self.servidor_graph.shutdown()
        self.servidor_llm.shutdown()


@pytest.fixture
async def escenario(db_engine, tmp_path):
    engine, db_name = db_engine
    e = _Escenario(async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession), db_name, tmp_path)
    try:
        await e.arrancar()
        yield e
    finally:
        for b in e.bridges:
            await b.detener()
        e.cerrar()


def _contenido(evento: dict) -> str:
    return str(evento.get("content") or evento.get("response") or evento)


def _resultados_de_herramienta(llm, herramienta: str, marca: str) -> list[dict]:
    salida = []
    for pedido in list(llm.requests):
        mensajes = pedido.get("messages") or []
        ultimo = max((i for i, m in enumerate(mensajes) if m.get("role") == "user"), default=-1)
        if ultimo < 0 or marca not in str(mensajes[ultimo].get("content") or ""):
            continue
        nombres = {}
        for m in mensajes[ultimo + 1 :]:
            for llamada in m.get("tool_calls") or []:
                nombres[llamada.get("id")] = (llamada.get("function") or {}).get("name")
            if m.get("role") == "tool" and (m.get("name") or nombres.get(m.get("tool_call_id"))) == herramienta:
                try:
                    resultado = json.loads(m.get("content") or "{}")
                except ValueError:
                    continue
                if resultado not in salida:
                    salida.append(resultado)
    return salida


class TestEscenarioIntegradoCronCorreoDosBots:
    async def test_cron_bot2_referencia_exacta_aprobacion_por_su_canal_y_un_solo_envio(self, escenario):
        e = escenario
        m = e.m
        u1, u2, u3 = (m["usuarios"][x] for x in ("u1", "u2", "u3"))
        a1, a2, a3 = (m["agentes"][x] for x in ("a1", "a2", "a3"))

        e.agregar(
            _correo("viejo1", "Cotización equipos", "Ana Ruiz", "aruiz@cliente.example", 90),
            _correo("viejo2", "Contrato marco", "Pedro Díaz", "pdiaz@cliente.example", 80),
        )
        inicio = time.monotonic()
        primeras = await e.disparar("u2", "u1", "u3")
        _medicion(caso="integrado", que="tres crons concurrentes", s=round(time.monotonic() - inicio, 2))
        e0 = primeras["u2"]
        assert [x["asunto"] for x in e0.metadata_json["resumen_correo"]["elementos"]] == [
            "Cotización equipos",
            "Contrato marco",
        ]
        aviso_e0 = await e.entregado(e.b2, e0.id)
        assert aviso_e0["clave"] == "u2-a2-b2" and aviso_e0["recipient_external_id"] == EXTERNO[("u2", 2)]
        aviso_u1 = await e.entregado(e.b1, primeras["u1"].id)
        aviso_u3 = await e.entregado(e.b2, primeras["u3"].id)
        assert aviso_u1["clave"] == "u1-a1-b1" and "MARCA-CRONU1" in aviso_u1["body"]
        assert aviso_u3["clave"] == "u3-a3-b2" and aviso_u3["recipient_external_id"] == EXTERNO[("u3", 2)]
        await e.entregado_en_bd(e0.id)

        s_b2 = await e.b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        s_b1 = await e.b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        s_u3 = await e.b2.sesion("u3-a3-b2", EXTERNO[("u3", 2)], AAD["u3"])
        s_u1 = await e.b1.sesion("u1-a1-b1", "29:u1-b1", AAD["u1"])
        recibido = await e.decir(s_b2, "recibido, gracias")
        assert recibido["type"] == "done", recibido

        e.agregar(
            _correo("factura", "Factura octubre", "Proveedor Uno", "facturas@proveedor.example", 50),
            _correo("reclamo", "Reclamo despacho", "Juan Soto", "jsoto@cliente.example", 40),
            _correo("caida", "Caída del servidor", "Juan Pérez", "jperez@cliente.example", 30, "INMEDIATO"),
        )
        e1 = (await e.disparar("u2"))["u2"]
        resumen = e1.metadata_json["resumen_correo"]["elementos"]
        assert [(x["posicion"], x["display_id"], x["asunto"]) for x in resumen] == [
            (1, "#853", "Caída del servidor"),
            (2, "#851", "Factura octubre"),
            (3, "#852", "Reclamo despacho"),
        ], resumen
        segundo = resumen[1]
        assert segundo["message_id"] == _mid("factura") and segundo["verificado"] is True
        assert segundo["internet_message_id"] == "<factura@cliente.example>"
        assert e.graph.bandeja[segundo["message_id"]]["internetMessageId"] == segundo["internet_message_id"]

        aviso_e1 = await e.entregado(e.b2, e1.id)
        await e.entregado_en_bd(e1.id)
        assert aviso_e1["recipient_external_id"] == EXTERNO[("u2", 2)]
        assert aviso_e1["bot_channel_id"] == m["canales"][2] and aviso_e1["origin"] == "cron"
        assert "| #851 |" in aviso_e1["body"] and "(relleno)" not in aviso_e1["body"]
        await e.vueltas_completas(2)
        for cron in (e0, e1):
            assert e.b1.de(task_id=cron.id) == [], "el resumen con destino bot 2 llegó al bot 1"
            assert len(e.b2.de(task_id=cron.id)) == 1
        async with e.maker() as s:
            filas = (
                (
                    await s.execute(
                        select(PublicChatNotification).where(PublicChatNotification.task_id.in_([e0.id, e1.id]))
                    )
                )
                .scalars()
                .all()
            )
        assert {(f.api_key_id, f.recipient_user_id, f.status) for f in filas} == {
            (m["id_clave"]["u2-a2-b2"], u2, "delivered")
        } and len(filas) == 2

        desde_bot1 = _contenido(await e.decir(s_b1, "quiero responder el segundo correo"))
        assert "ACLARACION" in desde_bot1 and '"estado": "sin_resumen"' in desde_bot1, (
            "el bot 1 resolvió un resumen que solo entregó el bot 2: " + desde_bot1
        )
        assert "Factura octubre" not in desde_bot1 and "Contrato marco" not in desde_bot1
        assert await e.aprobaciones() == []
        reinicio_b1 = await e.decir(s_b1, "/reset")
        assert reinicio_b1["type"] == "done" and "reiniciada" in _contenido(reinicio_b1), reinicio_b1

        marca_u1, marca_u3 = "MARCA-U1CHAT", "MARCA-U3CHAT"
        borrador, eco_u1, eco_u3 = await asyncio.gather(
            e.decir(s_b2, "quiero responder el segundo correo"),
            e.decir(s_u1, f"hola {marca_u1}"),
            e.decir(s_u3, f"hola {marca_u3}"),
        )
        _medicion(caso="integrado", que="borrador con referencia", s=borrador["duracion_s"])
        assert eco_u1["type"] == eco_u3["type"] == "done" and marca_u1 in _contenido(eco_u1)
        assert borrador["type"] == "done", borrador
        texto = _contenido(borrador)
        assert "¿Lo envío?" in texto and "Factura octubre" in texto and "facturas@proveedor.example" in texto, texto
        [referencia] = [
            r
            for r in _resultados_de_herramienta(e.llm, "ms365_correo_sync", "quiero responder el segundo correo")
            if r.get("estado") == "resuelto"
        ]
        assert referencia["ejecucion"]["task_id"] == e1.id, "resolvió otra ejecución del cron"
        assert referencia["correo"]["message_id"] == _mid("factura") and referencia["correo"]["display_id"] == "#851"
        [aprob] = await e.aprobaciones()
        assert (aprob["estado"], aprob["tipo_accion"], aprob["agent_id"]) == ("pendiente", "correo.responder", a2)
        assert aprob["propietario_user_id"] == u2 and aprob["conversacion_id"] == s_b2[0]
        assert aprob["contenido_json"]["mensaje_origen_id"] == _mid("factura")
        assert e.envios() == 0

        assert await e.pendientes(e.b1, "u2-a2-b1", s_b1) == [], "la tarjeta del bot 2 aparece en el bot 1"
        [tarjeta] = await e.pendientes(e.b2, "u2-a2-b2", s_b2)
        assert tarjeta["id"] == aprob["id"]
        assert await e.pendientes(e.b2, "u3-a3-b2", s_u3) == []
        intentos = {
            "sesion del bot 1": (await e.decidir(e.b1, "u2-a2-b1", s_b1, tarjeta, AAD["u2"]), 409, "otra_conversacion"),
            "clave del bot 1 con la sesion del bot 2": (
                await e.decidir(e.b1, "u2-a2-b1", s_b2, tarjeta, AAD["u2"]),
                403,
                "otra_conversacion",
            ),
            "otro AAD en el bot 2": (await e.decidir(e.b2, "u2-a2-b2", s_b2, tarjeta, AAD["u3"]), 403, "otro_usuario"),
            "U3 desde su sesion del bot 2": (
                await e.decidir(e.b2, "u3-a3-b2", s_u3, tarjeta, AAD["u3"]),
                404,
                "otro_usuario",
            ),
        }
        for nombre, (respuesta, estado, codigo) in intentos.items():
            assert (respuesta.status_code, _codigo(respuesta)) == (estado, codigo), (nombre, respuesta.text)
        assert [a["estado"] for a in await e.aprobaciones()] == ["pendiente"]

        inicio = time.monotonic()
        doble = await asyncio.gather(*(e.decidir(e.b2, "u2-a2-b2", s_b2, tarjeta, AAD["u2"]) for _ in range(2)))
        _medicion(
            caso="integrado",
            que="doble pulsación",
            s=round(time.monotonic() - inicio, 2),
            estados=[r.status_code for r in doble],
        )
        aceptadas = [r for r in doble if r.status_code == 200]
        assert len(aceptadas) == 1 and aceptadas[0].json()["estado"] == "aprobada", [r.text for r in doble]
        [rechazada] = [r for r in doble if r.status_code != 200]
        assert rechazada.status_code == 409 and _codigo(rechazada) in ("estado_invalido", "ya_decidida"), rechazada.text
        assert e.envios() == 0

        si_bot1 = await e.decir(s_b1, "sí")
        assert si_bot1["type"] == "done", si_bot1
        assert e.envios() == 0, "un «sí» en el bot 1 ejecutó la aprobación del bot 2"
        aprobaciones = await e.aprobaciones()
        assert [a["estado"] for a in aprobaciones if a["id"] == aprob["id"]] == ["aprobada"]
        del_bot1 = [a for a in aprobaciones if a["id"] != aprob["id"]]
        assert len(del_bot1) <= 1 and all(
            (a["conversacion_id"], a["estado"]) == (s_b1[0], "pendiente") for a in del_bot1
        ), del_bot1
        _medicion(
            caso="integrado",
            que="sí en el bot 1",
            aprobaciones_nuevas_en_bot1=len(del_bot1),
            respuesta=_contenido(si_bot1)[:160],
        )

        _medicion(caso="integrado", que="reinicio con la aprobación aprobada", s=await e.reiniciar())
        assert e.envios() == 0, "el reinicio ejecutó la aprobación sin el «sí» del usuario"

        inicio = time.monotonic()
        dos_si = await asyncio.gather(e.decir(s_b2, "sí"), e.decir(s_b2, "sí"))
        _medicion(
            caso="integrado",
            que="doble sí concurrente",
            s=round(time.monotonic() - inicio, 2),
            tipos=[x["type"] for x in dos_si],
        )

        async def _enviado():
            return e.envios() >= 1

        await _esperar(_enviado, 60, "el envío aprobado")
        tercero = await e.decir(s_b2, "sí")
        assert tercero["type"] == "done", tercero
        respuestas = [_contenido(x) for x in (*dos_si, tercero)]
        _medicion(caso="integrado", que="doble sí", respuestas=[r[:120] for r in respuestas])
        assert any("ya se envió" in r or "ya_enviado" in r for r in respuestas), respuestas
        repetidos = [
            r
            for r in _resultados_de_herramienta(e.llm, "ms365_mail_reply", "sí")
            if r.get("success") and not r.get("ya_enviado")
        ]
        assert not repetidos, "un «sí» repetido llegó a responder con éxito otra vez"
        assert e.envios() == 1, "la respuesta salió más de una vez"
        assert len(e.peticiones_graph("/createReply")) == 1
        assert not e.peticiones_graph("/reply") and not e.peticiones_graph("/replyAll")
        assert not e.peticiones_graph("/sendMail")
        [borrador_graph] = list(e.graph.borradores.values())
        assert borrador_graph["origen"] == _mid("factura") and borrador_graph["accion"] == "createReply"
        assert [d["emailAddress"]["address"] for d in borrador_graph["toRecipients"]] == ["facturas@proveedor.example"]
        assert TEXTO_RESPUESTA in borrador_graph["body"]["content"]
        estados = {a["id"]: a["estado"] for a in await e.aprobaciones()}
        assert estados[aprob["id"]] == "ejecutada"
        [final] = [a for a in await e.aprobaciones() if a["id"] == aprob["id"]]
        assert final["envio_estado"] in ("aceptada_proveedor", "confirmada", "entregada"), final["envio_estado"]
        assert all(v != "ejecutada" for k, v in estados.items() if k != aprob["id"])

        await e.vueltas_completas(2)
        for cron in (e0, e1):
            assert len(e.b2.de(task_id=cron.id)) == 1 and e.b1.de(task_id=cron.id) == [], (
                "aviso duplicado tras reinicio"
            )
        datos_u2 = [
            "Factura octubre",
            "Reclamo despacho",
            "Caída del servidor",
            "Cotización equipos",
            "Contrato marco",
            "facturas@proveedor.example",
            "factura@cliente.example",
            _mid("factura"),
            TEXTO_RESPUESTA,
        ]
        for pedido in list(e.llm.requests):
            if pedido.get("model") in (MODELO["a1"], MODELO["a3"]):
                volcado = json.dumps(pedido, ensure_ascii=False)
                assert not [d for d in datos_u2 if d in volcado], "el modelo de otro agente recibió datos de U2"
        assert any(p.get("model") == MODELO["a1"] for p in e.llm.requests)
        assert any(p.get("model") == MODELO["a3"] for p in e.llm.requests)
        async with e.maker() as s:
            avisos = (await s.execute(select(PublicChatNotification))).scalars().all()
            tareas = (await s.execute(select(Task).where(Task.agent_id.in_([a1, a3])))).scalars().all()
        for aviso in avisos:
            if aviso.recipient_user_id != u2:
                assert aviso.agent_id == {u1: a1, u3: a3}[aviso.recipient_user_id]
                assert not [d for d in datos_u2 if d in (aviso.body or "")], aviso.body
            else:
                assert aviso.agent_id == a2
        assert {a.recipient_user_id for a in avisos} == {u1, u2, u3}
        for tarea in tareas:
            volcado = json.dumps(tarea.metadata_json or {}, ensure_ascii=False) + (tarea.response or "")
            assert "resumen_correo" not in volcado and not [d for d in datos_u2 if d in volcado], tarea.id
        assert all((a["agent_id"], a["propietario_user_id"]) == (a2, u2) for a in await e.aprobaciones())
        for n in e.b1.recibidas:
            assert not [d for d in datos_u2 if d in json.dumps(n, ensure_ascii=False)], "el bot 1 vio datos del correo"
        for n in e.b2.recibidas:
            if n["clave"] == "u3-a3-b2":
                assert not [d for d in datos_u2 if d in json.dumps(n, ensure_ascii=False)]


def _cargar_plugin():
    spec = importlib.util.spec_from_file_location(f"correo_integrado_{uuid.uuid4().hex[:6]}", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Compatible:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.espacio = tmp_path / "agente-a2"
        (self.espacio / "work").mkdir(parents=True)
        self.graph = type(
            "GraphCompatible",
            (_GraphBandeja,),
            {"bandeja": {}, "borradores": {}, "marcados": [], "enviados": [], "peticiones": [], "modo": "202"},
        )
        correo = _correo("factura", "Factura octubre", "Proveedor Uno", "facturas@proveedor.example", 50)
        self.graph.bandeja[correo["id"]] = correo
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), self.graph)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.sesiones = {1: str(uuid.uuid4()), 2: str(uuid.uuid4())}
        self.claves = {1: str(uuid.uuid4()), 2: str(uuid.uuid4())}

    def tarea(self, bot: int, mensaje: str, respuesta_anterior: str = ""):
        task_id = str(uuid.uuid4())
        metadata = {
            "conversation": True,
            "source": "public_chat",
            "public_session_id": self.sesiones[bot],
            "thread_user_id": "u2",
            "public_chat_api_key_id": self.claves[bot],
        }
        entorno = {
            "HERMESHQ_APROBACION_CENTRAL": "0",
            "HERMES_SESSION_CWD": str(self.espacio),
            "HERMESHQ_TASK_PAYLOAD": json.dumps(
                {"task_id": task_id, "metadata": metadata, "mensaje": mensaje, "respuesta_anterior": respuesta_anterior}
            ),
        }
        for clave, valor in entorno.items():
            self.monkeypatch.setenv(clave, valor)
        plugin = _cargar_plugin()
        plugin.GRAPH_BASE = f"http://127.0.0.1:{self.servidor.server_address[1]}/v1"
        plugin._get_m365_token = lambda _u: ("token-delegado-simulado", "")
        return plugin

    def responder(self, plugin, aprobado: bool = False) -> dict:
        args = {"message_id": _mid("factura"), "body": TEXTO_RESPUESTA}
        if aprobado:
            args["aprobado_por_el_usuario"] = True
        return json.loads(plugin._reply_email_tool(args))

    def envios(self) -> int:
        return sum(1 for p in self.graph.peticiones if p["ruta"].split("?")[0].endswith("/send"))

    def cerrar(self):
        self.servidor.shutdown()


@pytest.fixture
def compatible(tmp_path, monkeypatch):
    c = _Compatible(tmp_path, monkeypatch)
    try:
        yield c
    finally:
        c.cerrar()


class TestModoCompatibleLigadoAlBot:
    def test_la_vista_previa_del_bot2_no_se_aprueba_desde_el_bot1(self, compatible):
        c = compatible
        previa = c.responder(c.tarea(2, "quiero responder el segundo correo"))
        assert previa["requiere_aprobacion"] is True and "borrador" in previa, previa
        borrador = previa["borrador"]
        assert c.envios() == 0

        for mensaje in ("sí", "envíalo"):
            c.sesiones[1] = str(uuid.uuid4())
            plugin = c.tarea(1, mensaje, "No hay un resumen de correos reciente entregado por este chat.")
            assert plugin.ejecutar_aprobacion_pendiente() is None
            intento = c.responder(plugin, aprobado=True)
            assert intento.get("success") is not True, (mensaje, intento)
            assert c.envios() == 0, f"«{mensaje}» en el bot 1 envió la respuesta preparada en el bot 2"

        aprobado = c.tarea(2, "sí", borrador)
        envio = aprobado.ejecutar_aprobacion_pendiente() or {"resultado": c.responder(aprobado, aprobado=True)}
        assert envio["resultado"].get("success") is True, envio
        assert c.envios() == 1
        for _ in range(3):
            otra_vez = c.tarea(2, "sí", "Listo: respondí el correo «Factura octubre».")
            assert otra_vez.ejecutar_aprobacion_pendiente() is None
            repetido = c.responder(otra_vez, aprobado=True)
            assert repetido.get("success") is not True and repetido.get("ya_enviado") is True, repetido
        assert c.envios() == 1, "un «sí» repetido volvió a enviar"

    def test_control_en_el_mismo_bot_el_si_posterior_envia(self, compatible):
        c = compatible
        c.responder(c.tarea(2, "quiero responder el segundo correo"))
        siguiente = c.tarea(2, "sí", "Hola, ¿en qué te ayudo?")
        assert c.responder(siguiente, aprobado=True).get("success") is True
        assert c.envios() == 1

    def test_sin_conversacion_conserva_el_comportamiento_anterior(self, compatible):
        c = compatible
        for bot in (1, 2):
            c.sesiones[bot] = ""
        c.responder(c.tarea(2, "quiero responder el segundo correo"))
        siguiente = c.tarea(1, "sí", "Hola")
        assert c.responder(siguiente, aprobado=True).get("success") is True
        assert c.envios() == 1
