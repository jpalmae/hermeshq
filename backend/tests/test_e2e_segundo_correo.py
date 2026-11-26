from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_correo_migrado import _GraphCorreo
from test_e2e_http_recuperacion import _Instance, _scratch_url
from test_e2e_reset_aislamiento import _clave_publica, _segundo_usuario, _sesion
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatNotification
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

TOKEN_SIMULADO = "token-graph-simulado-segundo-correo"
MARCA_CRON = "CRON-CORREO-E2E"
POLITICA = {
    "correo": {
        "operaciones": ["mail_folders", "mail_list", "mail_read", "mail_search", "directorio"],
        "carpetas": ["inbox", "sentitems"],
    }
}
TEXTO_RESPUESTA = "Gracias, lo reviso hoy."


def _mid(nombre: str) -> str:
    return "AAMkAGI2" + re.sub(r"[^A-Za-z0-9]", "", nombre) + "Q" * 70


def _correo(nombre, asunto, remitente, direccion, hace_min, categoria="CALIENTE"):
    return {
        "id": _mid(nombre),
        "internetMessageId": f"<{nombre}@cliente.example>",
        "parentFolderId": "INBOX-ID",
        "subject": asunto,
        "from": {"emailAddress": {"address": direccion, "name": remitente}},
        "toRecipients": [{"emailAddress": {"address": "admin@hq.example"}}],
        "ccRecipients": [],
        "receivedDateTime": (datetime.now(UTC) - timedelta(minutes=hace_min)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bodyPreview": f"Texto de {asunto}",
        "body": {"contentType": "text", "content": f"Cuerpo completo de «{asunto}»."},
        "isRead": False,
        "_categoria": categoria,
    }


class _GraphBandeja(_GraphCorreo):
    bandeja: dict = {}

    def _segun_select(self, datos: dict) -> dict:
        consulta = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        seleccion = (consulta.get("$select") or [""])[0]
        if not seleccion:
            return datos
        campos = set(seleccion.split(",")) | {"id"}
        return {k: v for k, v in datos.items() if k in campos}

    def do_GET(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        if ruta == "/me/mailFolders/inbox/messages":
            self._registrar()
            valor = sorted(
                (
                    self._segun_select({k: v for k, v in m.items() if not k.startswith("_")})
                    for m in type(self).bandeja.values()
                ),
                key=lambda m: m["receivedDateTime"],
                reverse=True,
            )
            return self._json(200, {"value": valor})
        if m := re.match(r"^/me/messages/([^/]+)$", ruta):
            self._registrar()
            mid = m.group(1)
            if mid.startswith("borrador-") and mid in type(self).borradores:
                return self._json(200, {"id": mid, "body": {"content": "<blockquote>hilo original</blockquote>"}})
            correo = type(self).bandeja.get(mid)
            if correo is None:
                return self._json(404, {"error": {"code": "ErrorItemNotFound"}})
            return self._json(200, self._segun_select({k: v for k, v in correo.items() if not k.startswith("_")}))
        return super().do_GET()


def _llamada(nombre: str, args: dict, indice: int) -> list[dict]:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
    llamada = {
        "index": 0,
        "id": f"call_{indice}_{uuid.uuid4().hex[:6]}",
        "type": "function",
        "function": {"name": nombre, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    return [
        {
            **base,
            "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [llamada]}, "finish_reason": None}],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _texto(contenido: str) -> list[dict]:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
    return [
        {
            **base,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": contenido}, "finish_reason": None}],
        },
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    ]


class _ModeloQueLlamaHerramientas(_FakeLLM):
    categorias: dict = {}
    ultima_respuesta: dict = {}

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
        chunks = self._cron(paso, previo) if MARCA_CRON in usuario else self._chat(usuario, paso, previo)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _cron(self, paso, previo):
        if paso == 0:
            return _llamada("ms365_correo_sync", {"accion": "ventana"}, paso)
        if paso == 1:
            return _llamada("ms365_correo_sync", {"accion": "plan"}, paso)
        if paso == 2:
            clasificaciones = {}
            for nuevo in previo.get("nuevos_sin_clasificar") or []:
                categoria = type(self).categorias.get(nuevo.get("subject"), "CALIENTE")
                clasificaciones[nuevo["token"]] = {
                    "display_category": categoria,
                    "base_temperature": categoria,
                    "operational_status": "PENDIENTE_DE_MÍ",
                    "requires_response": True,
                    "due_status": "SIN_PLAZO",
                    "action_required": "Responder",
                }
            return _llamada(
                "ms365_correo_sync",
                {"accion": "commit", "clasificaciones": clasificaciones, "verificar_leidos": False},
                paso,
            )
        return _texto(
            "Informe de correos\n\n## INMEDIATO\n\n| Correo | Remitente |\n|---|---|\n| (relleno) | x |\n\n"
            "## CALIENTE\n\n| Correo | Remitente |\n|---|---|\n| (relleno) | x |\n"
        )

    def _chat(self, usuario, paso, previo):
        propio = re.sub(r"(?s)\[[^\]]*\]", " ", usuario).strip()
        aprobacion = re.sub(r"[^a-záéíóú]", "", propio.lower()) in ("sí", "si")
        if aprobacion:
            if paso == 0 and type(self).ultima_respuesta:
                return _llamada("ms365_mail_reply", dict(type(self).ultima_respuesta), paso)
            return _texto("SEGUNDO-SI: " + json.dumps(previo, ensure_ascii=False)[:600])
        if paso == 0:
            args = {"accion": "referencia"}
            if m := re.search(r"EJECUCION=(\S+)", usuario):
                args["ejecucion"] = m.group(1)
                args["referencia"] = re.sub(r"EJECUCION=\S+", "", propio).strip()
            return _llamada("ms365_correo_sync", args, paso)
        if paso == 1:
            correo = previo.get("correo") or {}
            if previo.get("success") and correo.get("message_id"):
                type(self).ultima_respuesta = {"message_id": correo["message_id"], "body": TEXTO_RESPUESTA}
                return _llamada("ms365_mail_reply", dict(type(self).ultima_respuesta), paso)
            return _texto("ACLARACION: " + json.dumps(previo, ensure_ascii=False))
        if previo.get("borrador"):
            return _texto(str(previo["borrador"]) + "\n\n¿Lo envío?")
        return _texto("RESPUESTA: " + json.dumps(previo, ensure_ascii=False))


class _Escena:
    def __init__(self, maker, db_name, tmp_path):
        self.maker, self.db_name, self.tmp = maker, db_name, tmp_path
        self.workspace = tmp_path / "workspaces" / "agente-segundo-correo"
        (self.workspace / "work").mkdir(parents=True)
        (self.workspace / "work" / "correo_unificado_estado.json").write_text(
            json.dumps({"next_display_id": 849}), encoding="utf-8"
        )
        self.graph = type(
            "GraphBandeja",
            (_GraphBandeja,),
            {"bandeja": {}, "borradores": {}, "marcados": [], "enviados": [], "peticiones": [], "modo": "202"},
        )
        self.servidor_graph = ThreadingHTTPServer(("127.0.0.1", 0), self.graph)
        threading.Thread(target=self.servidor_graph.serve_forever, daemon=True).start()
        self.llm = type(
            "LLMSegundoCorreo",
            (_ModeloQueLlamaHerramientas,),
            {"delay": 0.0, "requests": [], "categorias": {}, "ultima_respuesta": {}},
        )
        self.servidor_llm = ThreadingHTTPServer(("127.0.0.1", 0), self.llm)
        threading.Thread(target=self.servidor_llm.serve_forever, daemon=True).start()
        self.instancia: _Instance | None = None

    def agregar(self, *correos):
        for correo in correos:
            self.graph.bandeja[correo["id"]] = correo
            self.llm.categorias[correo["subject"]] = correo["_categoria"]

    async def arrancar(self):
        llm_url = f"http://127.0.0.1:{self.servidor_llm.server_address[1]}/v1"
        self.instancia = _Instance(
            "backend-segundo-correo",
            _scratch_url(self.db_name),
            self.workspace,
            llm_url,
            self.tmp,
            extra_env={
                "HQ_E2E_INSTALACION_REAL": "1",
                "APPROVAL_CENTRAL_ENABLED": "true",
                "M365_AGENT_TOKEN_ENABLED": "false",
                "APPROVAL_RECONCILE_WAIT_SECONDS": "0",
                "HERMESHQ_ENTORNO": "aislado",
                "HERMESHQ_GRAPH_BASE_SIMULADO": f"http://127.0.0.1:{self.servidor_graph.server_address[1]}/v1",
                "HERMESHQ_GRAPH_TOKEN_SIMULADO": TOKEN_SIMULADO,
            },
        )
        await self.instancia.wait_ready()
        self.base = self.instancia.base
        async with self.maker() as s:
            self.admin_id = (await s.execute(text("SELECT id FROM users WHERE username = 'admin'"))).scalar_one()
            ajustes = await s.get(AppSettings, "default")
            if ajustes is None:
                ajustes = AppSettings(id="default")
                s.add(ajustes)
            ajustes.enabled_integration_packages = ["ms365-mail"]
            nodo = Node(name="nodo-segundo-correo", hostname="localhost")
            s.add(nodo)
            await s.flush()
            agente = Agent(
                node_id=nodo.id,
                name="agente-segundo-correo",
                slug="agente-segundo-correo",
                status="running",
                workspace_path=str(self.workspace),
                model="modelo-simulado",
                enabled_toolsets=["hermeshq_ms365_mail"],
                integration_configs={"ms365-mail": {}, "m365_graph": POLITICA},
                max_iterations=8,
                thinking_mode="off",
            )
            s.add(agente)
            await s.flush()
            s.add(AgentAssignment(agent_id=agente.id, user_id=self.admin_id))
            cron = ScheduledTask(
                agent_id=agente.id,
                name="Informe de correos",
                cron_expression="0 3 * * *",
                prompt=f"{MARCA_CRON}: arma el informe de correos con ms365_correo_sync",
                enabled=True,
                timezone="America/Santiago",
                owner_user_id=self.admin_id,
                approval_status="approved",
                notify_public_chat=True,
                next_run=datetime.now(UTC) + timedelta(days=1),
            )
            s.add(cron)
            await s.commit()
            self.agent_id, self.cron_id = agente.id, cron.id
        self.clave_a = await _clave_publica(self.maker, self.agent_id, self.admin_id)
        await self.sondear(self.clave_a)

    async def sondear(self, clave: str) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/api/public/chat/notifications", headers={"X-Api-Key": clave})
            assert r.status_code == 200, r.text
            avisos = r.json()
            for aviso in avisos:
                ack = await c.post(
                    f"{self.base}/api/public/chat/notifications/{aviso['id']}/ack", headers={"X-Api-Key": clave}
                )
                assert ack.status_code == 200, ack.text
        return avisos

    async def correr_cron(self) -> Task:
        async with self.maker() as s:
            previas = {
                t.id
                for t in (
                    await s.execute(
                        select(Task).where(Task.metadata_json["scheduled_task_id"].as_string() == self.cron_id)
                    )
                ).scalars()
            }
            await s.execute(
                text("UPDATE scheduled_tasks SET next_run = now() - interval '1 minute' WHERE id = :i"),
                {"i": self.cron_id},
            )
            await s.commit()
        for _ in range(240):
            async with self.maker() as s:
                filas = (
                    (
                        await s.execute(
                            select(Task).where(Task.metadata_json["scheduled_task_id"].as_string() == self.cron_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            nuevas = [t for t in filas if t.id not in previas]
            if nuevas and nuevas[0].status in ("completed", "failed", "cancelled"):
                assert nuevas[0].status == "completed", (nuevas[0].status, nuevas[0].error_message)
                return nuevas[0]
            await asyncio.sleep(0.5)
        raise AssertionError("el cron no terminó")

    async def entregar(self, clave: str | None = None) -> list[dict]:
        for _ in range(40):
            avisos = await self.sondear(clave or self.clave_a)
            if avisos:
                return avisos
            await asyncio.sleep(0.5)
        raise AssertionError("el aviso del cron no llegó al puente")

    async def decir(self, sesion, token, contenido) -> dict:
        async with (
            httpx.AsyncClient(timeout=240) as c,
            c.stream(
                "POST",
                f"{self.base}/api/public/chat/sessions/{sesion}/messages",
                headers={"X-Session-Token": token},
                json={"content": contenido},
            ) as r,
        ):
            assert r.status_code == 200, await r.aread()
            async for linea in r.aiter_lines():
                if linea.startswith("data: "):
                    evento = json.loads(linea[6:])
                    if evento.get("type") in ("done", "error", "cancelled"):
                        return evento
        raise AssertionError("el chat no terminó")

    def envios(self) -> int:
        return sum(1 for p in self.graph.peticiones if p["ruta"].split("?")[0].endswith("/send"))

    async def aprobaciones(self) -> list[dict]:
        async with self.maker() as s:
            return [
                dict(f)
                for f in (
                    await s.execute(
                        text(
                            "SELECT id, estado, tipo_accion, propietario_user_id, contenido_json FROM approvals ORDER BY creado_en"
                        )
                    )
                )
                .mappings()
                .all()
            ]

    def cerrar(self):
        if self.instancia:
            self.instancia.kill_group()
        self.servidor_graph.shutdown()
        self.servidor_llm.shutdown()


@pytest.fixture
async def escena(db_engine, tmp_path):
    engine, db_name = db_engine
    e = _Escena(async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession), db_name, tmp_path)
    try:
        await e.arrancar()
        yield e
    finally:
        e.cerrar()


def _contenido(evento: dict) -> str:
    return str(evento.get("content") or evento.get("response") or evento)


class TestSegundoCorreoDelResumen:
    async def test_segundo_correo_aprobacion_y_un_solo_envio(self, escena):
        escena.agregar(
            _correo("factura", "Factura octubre", "Proveedor Uno", "facturas@proveedor.example", 50),
            _correo("reclamo", "Reclamo despacho", "Juan Soto", "jsoto@cliente.example", 40),
            _correo("caida", "Caída del servidor", "Juan Pérez", "jperez@cliente.example", 30, "INMEDIATO"),
        )
        cron = await escena.correr_cron()
        resumen = cron.metadata_json["resumen_correo"]["elementos"]
        assert [(e["posicion"], e["display_id"], e["asunto"]) for e in resumen] == [
            (1, "#851", "Caída del servidor"),
            (2, "#849", "Factura octubre"),
            (3, "#850", "Reclamo despacho"),
        ]
        assert resumen[1]["message_id"] == _mid("factura") and resumen[1]["verificado"] is True
        assert resumen[1]["internet_message_id"] == "<factura@cliente.example>"
        [aviso] = await escena.entregar()
        assert "| #851 |" in aviso["body"] and "| #849 |" in aviso["body"] and "(relleno)" not in aviso["body"]

        sesion, token = await _sesion(escena.base, escena.clave_a, "aad-usuario-a")
        borrador = await escena.decir(sesion, token, "quiero trabajar con el segundo correo")
        assert borrador["type"] == "done", borrador
        texto = _contenido(borrador)
        assert "¿Lo envío?" in texto and "Factura octubre" in texto and "facturas@proveedor.example" in texto, texto
        [aprob] = await escena.aprobaciones()
        assert aprob["estado"] == "pendiente" and aprob["tipo_accion"] == "correo.responder"
        assert aprob["contenido_json"]["mensaje_origen_id"] == _mid("factura")
        assert escena.envios() == 0

        enviado = await escena.decir(sesion, token, "sí")
        assert enviado["type"] == "done", enviado
        assert escena.envios() == 1
        [borrador_graph] = [b for b in escena.graph.borradores.values()]
        assert borrador_graph["origen"] == _mid("factura") and borrador_graph["accion"] == "createReply"
        assert [d["emailAddress"]["address"] for d in borrador_graph["toRecipients"]] == ["facturas@proveedor.example"]
        assert TEXTO_RESPUESTA in borrador_graph["body"]["content"]
        assert [a["estado"] for a in await escena.aprobaciones()] == ["ejecutada"]

        repetido = await escena.decir(sesion, token, "sí")
        assert repetido["type"] == "done", repetido
        assert escena.envios() == 1, "un segundo «sí» no puede volver a enviar"
        assert "ya se envió" in _contenido(repetido) or "ya_enviado" in _contenido(repetido), _contenido(repetido)

        inexistente = await escena.decir(sesion, token, "y ahora el décimo correo")
        assert "ACLARACION" in _contenido(inexistente) and "no existe la posición 10" in _contenido(inexistente)
        ambiguo = await escena.decir(sesion, token, "mejor el de Juan")
        texto = _contenido(ambiguo)
        assert "ACLARACION" in texto and '"estado": "ambiguo"' in texto, texto
        assert "Caída del servidor" in texto and "Reclamo despacho" in texto
        assert escena.envios() == 1 and len(await escena.aprobaciones()) == 1

    async def test_ambiguo_entre_resumenes_eliminado_viejo_reset_y_otro_usuario(self, escena):
        escena.agregar(
            _correo("uno", "Cotización equipos", "Ana Ruiz", "aruiz@cliente.example", 90),
            _correo("dos", "Contrato marco", "Pedro Díaz", "pdiaz@cliente.example", 80),
        )
        primera = await escena.correr_cron()
        await escena.entregar()
        escena.agregar(
            _correo("tres", "Renovación licencias", "Marta Gil", "mgil@cliente.example", 5),
            _correo("cuatro", "Visita técnica", "Luis Mora", "lmora@cliente.example", 4),
        )
        segunda = await escena.correr_cron()
        await escena.entregar()
        assert [e["display_id"] for e in segunda.metadata_json["resumen_correo"]["elementos"]] == ["#851", "#852"]

        sesion, token = await _sesion(escena.base, escena.clave_a, "aad-usuario-a")
        ambiguo = _contenido(await escena.decir(sesion, token, "respóndele al segundo"))
        assert '"estado": "ambiguo_resumenes"' in ambiguo, ambiguo
        assert primera.id in ambiguo and segunda.id in ambiguo

        elegido = await escena.decir(sesion, token, f"el segundo EJECUCION={primera.id}")
        assert "Contrato marco" in _contenido(elegido) and "¿Lo envío?" in _contenido(elegido), _contenido(elegido)
        del escena.graph.bandeja[_mid("cuatro")]
        eliminado = _contenido(await escena.decir(sesion, token, f"el segundo EJECUCION={segunda.id}"))
        assert '"eliminado_o_movido": true' in eliminado and "Visita técnica" in eliminado, eliminado
        assert len(await escena.aprobaciones()) == 1

        async with escena.maker() as s:
            await s.execute(
                text(
                    "UPDATE public_chat_notifications SET delivered_at = delivered_at - interval "
                    "'13 hours' WHERE recipient_user_id = :u"
                ),
                {"u": escena.admin_id},
            )
            await s.commit()
        viejo = _contenido(await escena.decir(sesion, token, "el primero"))
        assert '"estado": "sin_resumen"' in viejo, viejo
        async with escena.maker() as s:
            await s.execute(
                text(
                    "UPDATE public_chat_notifications SET delivered_at = delivered_at + interval "
                    "'13 hours' WHERE recipient_user_id = :u"
                ),
                {"u": escena.admin_id},
            )
            await s.commit()
        vigente = _contenido(await escena.decir(sesion, token, "el primero"))
        assert "Renovación licencias" in vigente and "¿Lo envío?" in vigente, vigente
        assert (await escena.decir(sesion, token, "/reset"))["type"] == "done"
        tras_reset = _contenido(await escena.decir(sesion, token, "quiero trabajar con el primer correo"))
        assert '"estado": "sin_resumen"' in tras_reset, tras_reset

        usuario_b = await _segundo_usuario(escena.maker, escena.agent_id)
        clave_b = await _clave_publica(escena.maker, escena.agent_id, usuario_b)
        sesion_b, token_b = await _sesion(escena.base, clave_b, "aad-usuario-b")
        ajeno = _contenido(await escena.decir(sesion_b, token_b, "quiero trabajar con el segundo correo"))
        assert '"estado": "sin_resumen"' in ajeno, ajeno
        explicito = _contenido(await escena.decir(sesion_b, token_b, f"el segundo EJECUCION={primera.id}"))
        assert '"estado": "sin_resumen"' in explicito and "Contrato marco" not in explicito, explicito
        assert all(a["propietario_user_id"] == escena.admin_id for a in await escena.aprobaciones())
        assert escena.envios() == 0
        async with escena.maker() as s:
            avisos = (await s.execute(select(PublicChatNotification))).scalars().all()
        assert {a.recipient_user_id for a in avisos} == {escena.admin_id}
