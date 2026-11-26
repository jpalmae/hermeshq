from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_correo_central import _Graph, _modo, _Mundo

pytestmark = [pytest.mark.integration, requires_database]

POLITICA = {
    "correo": {
        "operaciones": ["mail_folders", "mail_list", "mail_read", "mail_search", "mail_mark_read", "directorio"],
        "carpetas": ["inbox", "sentitems"],
    }
}
ADJUNTO = b"contenido del adjunto original"
MENSAJES = {
    "orig-00001": {
        "parentFolderId": "INBOX-ID",
        "subject": "Presupuesto 2027",
        "from": {"emailAddress": {"address": "cliente@externo.example", "name": "Cliente"}},
        "toRecipients": [
            {"emailAddress": {"address": "admin@hq.example"}},
            {"emailAddress": {"address": "colega@hq.example"}},
        ],
        "ccRecipients": [{"emailAddress": {"address": "jefa@externo.example"}}],
    },
    "fuera-00001": {
        "parentFolderId": "OTRA-CARPETA",
        "subject": "Privado",
        "from": {"emailAddress": {"address": "x@x.example"}},
        "toRecipients": [],
    },
}


class _GraphCorreo(_Graph):
    borradores: dict = {}
    marcados: list = []

    def _json(self, estado, datos):
        cuerpo = json.dumps(datos).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        if m := re.match(r"^/me/mailFolders/(inbox|sentitems)$", ruta):
            self._registrar()
            return self._json(200, {"id": {"inbox": "INBOX-ID", "sentitems": "SENT-ID"}[m.group(1)]})
        if ruta == "/me/mailFolders":
            self._registrar()
            return self._json(
                200, {"value": [{"id": "INBOX-ID", "displayName": "Bandeja de entrada", "childFolderCount": 1}]}
            )
        if re.match(r"^/me/mailFolders/[^/]+/childFolders$", ruta):
            self._registrar()
            return self._json(200, {"value": [{"id": "SUB-ID", "displayName": "Proyectos", "childFolderCount": 0}]})
        if m := re.match(r"^/me/messages/([^/]+)/attachments/([^/]+)/\$value$", ruta):
            self._registrar()
            self.send_response(200)
            self.send_header("Content-Length", str(len(ADJUNTO)))
            self.end_headers()
            self.wfile.write(ADJUNTO)
            return
        if m := re.match(r"^/me/messages/([^/]+)/attachments$", ruta):
            self._registrar()
            return self._json(
                200,
                {
                    "value": [
                        {
                            "id": "adj-1",
                            "name": "presupuesto.pdf",
                            "size": len(ADJUNTO),
                            "isInline": False,
                            "contentType": "application/pdf",
                        }
                    ]
                },
            )
        if m := re.match(r"^/me/messages/([^/]+)$", ruta):
            self._registrar()
            msg = MENSAJES.get(m.group(1))
            return self._json(200, {"id": m.group(1), **msg}) if msg else self._json(404, {"error": {}})
        if ruta in ("/me/people", "/users", "/me"):
            self._registrar()
            if ruta == "/me":
                return self._json(200, {"mail": "admin@hq.example", "userPrincipalName": "admin@hq.example"})
            return self._json(
                200,
                {
                    "value": [
                        {
                            "displayName": "Carolina Marin",
                            "mail": "carolina@hq.example",
                            "userPrincipalName": "carolina@hq.example",
                        }
                    ]
                },
            )
        if "/mailFolders/inbox/messages" in ruta and "%24search" in self.path or "$search" in self.path:
            self._registrar()
            return self._json(
                200,
                {
                    "value": [
                        {
                            "id": "orig-00001",
                            "parentFolderId": "INBOX-ID",
                            "receivedDateTime": "2026-11-01T10:00:00Z",
                            "from": MENSAJES["orig-00001"]["from"],
                            "toRecipients": [
                                {"emailAddress": {"address": "carolina@hq.example", "name": "Carolina Marin"}}
                            ],
                        }
                    ]
                },
            )
        return super().do_GET()

    def do_PATCH(self):
        largo = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(largo) or b"{}")
        self._registrar(cuerpo)
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        mid = ruta.rsplit("/", 1)[-1]
        if mid.startswith("borrador-"):
            type(self).borradores[mid].update(cuerpo)
        else:
            type(self).marcados.append((mid, cuerpo))
        return self._json(200, {})

    def do_POST(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        if m := re.match(r"^/me/messages/([^/]+)/(createReply|createReplyAll|createForward)$", ruta):
            largo = int(self.headers.get("Content-Length") or 0)
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            self._registrar(cuerpo)
            bid = f"borrador-{len(type(self).borradores) + 1}"
            type(self).borradores[bid] = {"origen": m.group(1), "accion": m.group(2), "inicial": cuerpo, "adjuntos": []}
            return self._json(201, {"id": bid, "body": {"content": "<blockquote>hilo original</blockquote>"}})
        if m := re.match(r"^/me/messages/(borrador-\d+)/attachments$", ruta):
            largo = int(self.headers.get("Content-Length") or 0)
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            self._registrar(cuerpo)
            type(self).borradores[m.group(1)]["adjuntos"].append(cuerpo)
            return self._json(201, {"id": "a"})
        if m := re.match(r"^/me/messages/(borrador-\d+)/send$", ruta):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._registrar()
            if type(self).modo == "timeout_tras_aceptar":
                type(self).enviados.append({"borrador": m.group(1)})
                self.connection.close()
                return
            type(self).enviados.append(dict(type(self).borradores[m.group(1)], id=m.group(1)))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_POST()


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type("GraphCorreo", (_GraphCorreo,), {"borradores": {}, "marcados": []})
    m = _Mundo(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        graph_cls=graph,
        politica=POLITICA,
    )
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


def _envios(m: _Mundo) -> int:
    return sum(1 for p in m.graph.peticiones if p["ruta"].endswith("/send") or p["ruta"].endswith("/sendMail"))


async def _plugin(m: _Mundo):
    task_id, lease = await m.tarea()
    return task_id, lease, m.plugin_para(task_id, lease)


async def _llamar(plugin, herramienta, **args):
    return json.loads(await asyncio.to_thread(getattr(plugin, herramienta), args))


class TestLecturasPorBroker:
    async def test_lecturas_sin_token_ni_aprobacion(self, mundo):
        _, _, plugin = await _plugin(mundo)
        carpetas = await _llamar(plugin, "_list_folders_tool")
        assert carpetas["success"] is True, carpetas
        assert {"Bandeja de entrada", "Proyectos"} <= {
            c.get("name") or c.get("displayName") or c.get("path", "").split("/")[-1] for c in carpetas["folders"]
        }
        varios = await _llamar(plugin, "_get_emails_tool", message_ids=["orig-00001", "fuera-00001"])
        assert varios["success"] is True
        por_id = {m.get("id") or m.get("message_id"): m for m in varios["messages"]}
        assert "error" not in por_id["orig-00001"] and "error" in por_id["fuera-00001"]
        personas = await _llamar(plugin, "_find_person_tool", name="Carolina Marin")
        assert personas["success"] is True, personas
        assert "carolina@hq.example" in json.dumps(personas)
        adjuntos = await _llamar(plugin, "_mail_attachments_tool", message_id="orig-00001", download=True)
        assert adjuntos["success"] is True, adjuntos
        guardado = mundo.workspace / adjuntos["descargados"][0]["local_path"]
        assert guardado.read_bytes() == ADJUNTO
        assert _envios(mundo) == 0
        assert all(p["auth"] == "Bearer token-graph-del-servidor" for p in mundo.graph.peticiones)
        async with mundo.maker() as s:
            from sqlalchemy import text

            assert (await s.execute(text("SELECT count(*) FROM approvals"))).scalar() == 0
            assert (await s.execute(text("SELECT count(*) FROM external_action_records"))).scalar() == 0

    async def test_seguimiento_de_bandeja_por_broker(self, mundo):
        _, _, plugin = await _plugin(mundo)
        no_leidos, respondidos = await asyncio.to_thread(plugin._estado_no_leidos)
        assert no_leidos is not None


class TestMarcarLeido:
    async def test_marcar_leido_es_reversible_sin_aprobacion_pero_con_propietario_y_fencing(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        r = await _llamar(plugin, "_mark_read_tool", message_id="orig-00001", is_read=True)
        assert r["success"] is True, r
        assert mundo.graph.marcados == [("orig-00001", {"isRead": True})]
        fuera = await _llamar(plugin, "_mark_read_tool", message_id="fuera-00001", is_read=True)
        assert fuera["success"] is False or fuera.get("fallidos")
        assert len(mundo.graph.marcados) == 1
        async with httpx.AsyncClient() as c:
            otro_campo = await c.post(
                f"{mundo.backend.base}/api/internal/control/m365/graph",
                headers=plugin._cabeceras_control(),
                json={
                    "tarea_id": task_id,
                    "metodo": "PATCH",
                    "ruta": "/me/messages/orig-00001",
                    "cuerpo_json": {"isRead": True, "subject": "cambiado"},
                },
            )
        assert otro_campo.status_code == 403 and otro_campo.json()["detail"]["motivo"] == "solo_se_permite_isRead"
        async with mundo.maker() as s:
            from sqlalchemy import text

            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 s' WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        vencido = await _llamar(plugin, "_mark_read_tool", message_id="orig-00001", is_read=False)
        assert vencido["success"] is False or vencido.get("fallidos")
        assert len(mundo.graph.marcados) == 1


class TestResponderYReenviar:
    async def test_responder_a_todos_con_aprobacion_envia_exactamente_lo_aprobado(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        pedido = await _llamar(
            plugin, "_reply_email_tool", message_id="orig-00001", body="Gracias, lo reviso.", reply_all=True
        )
        assert pedido["requiere_aprobacion"] is True, pedido
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["tipo_accion"] == "correo.responder"
        assert fila["contenido_json"]["para"] == ["cliente@externo.example"]
        assert fila["contenido_json"]["cc"] == ["colega@hq.example", "jefa@externo.example"]
        assert _envios(mundo) == 0 and not mundo.graph.borradores
        assert (await mundo.decidir(pedido["aprobacion_id"])).status_code == 200
        r = await _llamar(
            plugin,
            "_reply_email_tool",
            message_id="orig-00001",
            body="Gracias, lo reviso.",
            reply_all=True,
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert r["success"] is True and r["entregado"] is False, r
        assert _envios(mundo) == 1
        borrador = mundo.graph.enviados[0]
        assert borrador["accion"] == "createReplyAll" and borrador["origen"] == "orig-00001"
        assert [d["emailAddress"]["address"] for d in borrador["toRecipients"]] == ["cliente@externo.example"]
        assert [d["emailAddress"]["address"] for d in borrador["ccRecipients"]] == [
            "colega@hq.example",
            "jefa@externo.example",
        ]
        assert borrador["body"]["content"].startswith(fila["contenido_json"]["cuerpo_html"])
        assert borrador["body"]["content"].endswith("<blockquote>hilo original</blockquote>")
        assert {"name": "x-hermeshq-aprobacion", "value": pedido["aprobacion_id"]} in borrador["inicial"]["message"][
            "internetMessageHeaders"
        ]
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["confirmada"]

    async def test_responder_cambiado_tras_aprobar_o_fuera_de_carpeta_no_envia(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        pedido = await _llamar(plugin, "_reply_email_tool", message_id="orig-00001", body="Texto aprobado")
        await mundo.decidir(pedido["aprobacion_id"])
        cambiado = await _llamar(
            plugin,
            "_reply_email_tool",
            message_id="orig-00001",
            body="Texto distinto",
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert cambiado["requiere_aprobacion"] is True and "cambió" in cambiado["error"]
        fuera = await _llamar(plugin, "_reply_email_tool", message_id="fuera-00001", body="hola")
        assert fuera["success"] is False
        assert _envios(mundo) == 0 and not mundo.graph.borradores

    async def test_reenviar_con_aprobacion_y_destinatarios_alterados(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        pedido = await _llamar(
            plugin,
            "_forward_email_tool",
            message_id="orig-00001",
            to="socio@externo.example",
            cc="jefa@externo.example",
            comment="Para tu revisión",
        )
        assert pedido["requiere_aprobacion"] is True, pedido
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["contenido_json"]["adjuntos_originales"] == ["presupuesto.pdf"]
        await mundo.decidir(pedido["aprobacion_id"])
        alterado = await _llamar(
            plugin,
            "_forward_email_tool",
            message_id="orig-00001",
            to="otro@externo.example",
            cc="jefa@externo.example",
            comment="Para tu revisión",
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert alterado["requiere_aprobacion"] is True and _envios(mundo) == 0
        nuevo_id = alterado["aprobacion_id"]
        await mundo.decidir(nuevo_id)
        ok = await _llamar(
            plugin,
            "_forward_email_tool",
            message_id="orig-00001",
            to="otro@externo.example",
            cc="jefa@externo.example",
            comment="Para tu revisión",
            aprobacion_id=nuevo_id,
        )
        assert ok["success"] is True, ok
        enviado = mundo.graph.enviados[0]
        assert enviado["accion"] == "createForward" and enviado["inicial"]["comment"] == "Para tu revisión"
        assert [d["emailAddress"]["address"] for d in enviado["toRecipients"]] == ["otro@externo.example"]
        assert _envios(mundo) == 1

    async def test_sin_aprobacion_no_hay_ruta_para_responder_ni_reenviar(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        async with httpx.AsyncClient() as c:
            for ruta in (
                "/me/messages/orig-00001/createReply",
                "/me/messages/orig-00001/forward",
                "/me/messages/orig-00001/reply",
                "/me/sendMail",
            ):
                r = await c.post(
                    f"{mundo.backend.base}/api/internal/control/m365/graph",
                    headers=plugin._cabeceras_control(),
                    json={"tarea_id": task_id, "metodo": "POST", "ruta": ruta, "cuerpo_json": {}},
                )
                assert r.status_code == 403, (ruta, r.text)
        assert not mundo.graph.borradores and _envios(mundo) == 0

    async def test_timeout_al_enviar_respuesta_queda_incierto_y_no_se_repite(self, mundo):
        task_id, lease, plugin = await _plugin(mundo)
        pedido = await _llamar(plugin, "_reply_email_tool", message_id="orig-00001", body="Respuesta")
        await mundo.decidir(pedido["aprobacion_id"])
        mundo.graph.modo = "timeout_tras_aceptar"
        r = await _llamar(
            plugin,
            "_reply_email_tool",
            message_id="orig-00001",
            body="Respuesta",
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert r["estado_envio"] == "desconocida"
        mundo.graph.modo = "202"
        otra = await _llamar(
            plugin,
            "_reply_email_tool",
            message_id="orig-00001",
            body="Respuesta",
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert otra["success"] is False
        assert _envios(mundo) == 1

    async def test_sin_tokens_desactivar_no_abre_una_ruta_legacy(self, mundo):
        await _modo(mundo, mundo.agent_id, "desactivada")
        task_id, lease, plugin = await _plugin(mundo)
        cab = plugin._cabeceras_control()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{mundo.backend.base}/api/internal/control/m365/correo/enviar",
                headers=cab,
                json={"tarea_id": task_id, "aprobacion_id": "inexistente", "contenido": {}},
            )
            token = await c.get(
                f"{mundo.backend.base}/api/internal/control/m365/agent-token",
                params={"user_id": mundo.admin_id},
                headers={k: v for k, v in cab.items() if k != "Content-Type"},
            )
        assert r.status_code == 404 and r.json()["detail"]["motivo"] == "no_encontrada"
        assert token.status_code == 410
