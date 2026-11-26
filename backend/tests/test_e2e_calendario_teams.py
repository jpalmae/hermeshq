from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_correo_central import _Graph, _login, _login_como, _Mundo

pytestmark = [pytest.mark.integration, requires_database]

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages"
POLITICA = {
    "calendario": {"operaciones": ["lectura"]},
    "teams": {"operaciones": ["lectura"]},
    "correo": {"operaciones": ["mail_list"], "carpetas": ["inbox"]},
}
EVENTO = {
    "id": "evt-00001",
    "subject": "Comité",
    "start": {"dateTime": "2026-11-20T10:00:00", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-20T11:00:00", "timeZone": "UTC"},
    "attendees": [{"emailAddress": {"address": "socia@externo.example"}, "type": "required"}],
    "organizer": {"emailAddress": {"address": "otra@externo.example"}},
}


class _GraphCalTeams(_Graph):
    miembros: list = ["admin@hq.example", "colega@hq.example"]
    escrituras: list = []
    respuesta_crear = 201

    def _json(self, estado, datos):
        cuerpo = json.dumps(datos).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cuerpo(self):
        largo = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(largo) or b"{}") if largo else None

    lista_falla = False

    def do_GET(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        self._registrar()
        if ruta == "/me/events" and type(self).lista_falla:
            return self._json(503, {"error": {"code": "serviceNotAvailable"}})
        if ruta in ("/me/calendarView", "/me/events"):
            return self._json(200, {"value": [EVENTO]})
        if re.match(r"^/me/events/[^/]+$", ruta):
            return self._json(200, EVENTO)
        if ruta == "/me/joinedTeams":
            return self._json(200, {"value": [{"id": "team-1", "displayName": "Finanzas"}]})
        if re.match(r"^/me/chats/[^/]+/members$", ruta):
            return self._json(200, {"value": [{"email": m, "userId": m} for m in type(self).miembros]})
        if m := re.match(r"^/me/chats/([^/]+)$", ruta):
            miembros = [{"email": x, "userId": x, "displayName": x} for x in type(self).miembros]
            return self._json(200, {"id": m.group(1), "chatType": "oneOnOne", "topic": None, "members": miembros})
        if re.match(r"^/me/chats/[^/]+/messages$", ruta):
            return self._json(
                200, {"value": [{"id": "m1", "body": {"content": "hola"}, "from": {"user": {"displayName": "Colega"}}}]}
            )
        if ruta == "/me/chats":
            return self._json(
                200,
                {
                    "value": [
                        {
                            "id": "19:chat-directo@thread.v2",
                            "chatType": "oneOnOne",
                            "members": [{"userId": "user-colega"}, {"userId": "user-admin"}],
                        }
                    ]
                },
            )
        if ruta == "/users":
            return self._json(
                200,
                {
                    "value": [
                        {
                            "id": "user-colega",
                            "displayName": "Colega",
                            "mail": "colega@hq.example",
                            "userPrincipalName": "colega@hq.example",
                        }
                    ]
                },
            )
        if ruta == "/me":
            return self._json(200, {"id": "user-admin", "mail": "admin@hq.example"})
        return super().do_GET()

    def _escribir(self, estado, datos=None):
        cuerpo = self._cuerpo()
        self._registrar(cuerpo)
        if type(self).modo == "timeout_tras_aceptar":
            type(self).escrituras.append((self.command, self.path, cuerpo))
            self.connection.close()
            return
        type(self).escrituras.append((self.command, self.path, cuerpo))
        if datos is None:
            self.send_response(estado)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._json(estado, datos)

    def do_POST(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        if ruta == "/me/events":
            if type(self).respuesta_crear == 400:
                return self._escribir(400, {"error": {"message": "Hora inválida"}})
            return self._escribir(201, {"id": f"evt-nuevo-{len(type(self).escrituras) + 1}"})
        if re.match(r"^/me/events/[^/]+/(accept|decline|tentativelyAccept)$", ruta):
            return self._escribir(202)
        if re.match(r"^/me/chats/[^/]+/messages$", ruta):
            return self._escribir(201, {"id": f"msg-{len(type(self).escrituras) + 1}"})
        return super().do_POST()

    def do_PATCH(self):
        return self._escribir(200, {"id": "evt-00001"})

    def do_DELETE(self):
        return self._escribir(204)


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphCT", (_GraphCalTeams,), {"escrituras": [], "miembros": ["admin@hq.example", "colega@hq.example"]}
    )
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


def _cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(
        f"{nombre}_{uuid.uuid4().hex[:6]}", RAIZ / nombre / "plugin" / "__init__.py"
    )
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    def _prohibido(*_a, **_k):
        raise AssertionError(f"{nombre} pidió un token de Graph en modo central")

    modulo._get_m365_token = _prohibido
    return modulo


async def _plugins(m: _Mundo, usuario_id=None):
    task_id, lease = await m.tarea(usuario_id=usuario_id)
    m.plugin_para(task_id, lease, usuario_id=usuario_id)
    return task_id, lease, _cargar("ms365-calendar"), _cargar("ms365-teams")


async def _llamar(plugin, herramienta, **args):
    return json.loads(await asyncio.to_thread(getattr(plugin, herramienta), args))


def _escrituras(m: _Mundo, metodo=None):
    return [e for e in m.graph.escrituras if metodo is None or e[0] == metodo]


async def _aprobar(m: _Mundo, respuesta: dict):
    assert respuesta.get("requiere_aprobacion") is True, respuesta
    r = await m.decidir(respuesta["aprobacion_id"])
    assert r.status_code == 200, r.text
    return respuesta["aprobacion_id"]


class TestLecturas:
    async def test_lecturas_de_calendario_y_teams_sin_token(self, mundo):
        _, _, cal, teams = await _plugins(mundo)
        eventos = await _llamar(cal, "_list_events_tool", start="2026-11-01T00:00:00Z", end="2026-11-30T00:00:00Z")
        assert eventos["success"] is True, eventos
        assert "Comité" in json.dumps(eventos, ensure_ascii=False)
        evento = await _llamar(cal, "_get_event_tool", event_id="evt-00001")
        assert evento["success"] is True, evento
        equipos = await _llamar(teams, "_list_teams_tool")
        assert equipos["success"] is True and "Finanzas" in json.dumps(equipos, ensure_ascii=False)
        mensajes = await _llamar(teams, "_get_chat_messages_tool", chat_id="19:chat-directo@thread.v2")
        assert mensajes["success"] is True, mensajes
        assert not _escrituras(mundo)
        assert all(p["auth"] == "Bearer token-graph-del-servidor" for p in mundo.graph.peticiones)

    async def test_sin_politica_de_lectura_se_niega(self, mundo):
        async with mundo.maker() as s:
            await s.execute(
                text("UPDATE agents SET integration_configs = :c WHERE id = :a"),
                {
                    "c": json.dumps({"m365_graph": {"correo": {"operaciones": ["mail_list"], "carpetas": ["inbox"]}}}),
                    "a": mundo.agent_id,
                },
            )
            await s.commit()
        _, _, cal, teams = await _plugins(mundo)
        eventos = await _llamar(cal, "_list_events_tool", start="2026-11-01T00:00:00Z", end="2026-11-30T00:00:00Z")
        assert eventos["success"] is False
        equipos = await _llamar(teams, "_list_teams_tool")
        assert equipos["success"] is False


class TestCalendario:
    async def test_crear_con_asistentes_exige_aprobacion_y_ejecuta_exactamente_lo_aprobado(self, mundo):
        task_id, _, cal, _ = await _plugins(mundo)
        args = {
            "subject": "Revisión trimestral",
            "start": "2026-11-25T15:00:00",
            "end": "2026-11-25T16:00:00",
            "timezone": "UTC",
            "attendees": "socia@externo.example",
        }
        pedido = await _llamar(cal, "_create_event_tool", **args)
        assert not _escrituras(mundo)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["tipo_accion"] == "graph.calendario"
        assert "socia@externo.example" in fila["vista_previa"] and "Revisión trimestral" in fila["vista_previa"]
        await _aprobar(mundo, pedido)
        r = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "POST")) == 1
        metodo, ruta, cuerpo = _escrituras(mundo)[0]
        assert cuerpo.pop("transactionId") == f"hermeshq-{pedido['aprobacion_id']}"
        assert cuerpo == fila["contenido_json"]["cuerpo"]
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutada", "confirmada")
        otra = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=pedido["aprobacion_id"])
        assert otra["success"] is True and otra.get("ya_ejecutada"), otra
        assert len(_escrituras(mundo, "POST")) == 1
        assert (await mundo.eventos_aprobacion(pedido["aprobacion_id"])).count("ejecutada") == 1

    async def test_modificar_cambiado_tras_aprobar_pide_aprobacion_nueva(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_update_event_tool", event_id="evt-00001", subject="Comité (movido)")
        await _aprobar(mundo, pedido)
        cambiado = await _llamar(
            cal,
            "_update_event_tool",
            event_id="evt-00001",
            subject="Comité (cancelado)",
            aprobacion_id=pedido["aprobacion_id"],
        )
        assert cambiado["requiere_aprobacion"] is True and cambiado["aprobacion_id"] != pedido["aprobacion_id"]
        assert len(_escrituras(mundo, "PATCH")) == 1
        ok = await _llamar(
            cal,
            "_update_event_tool",
            event_id="evt-00001",
            subject="Comité (cancelado)",
            aprobacion_id=await _aprobar(mundo, cambiado),
        )
        assert ok["success"] is True and _escrituras(mundo, "PATCH")[-1][2] == {"subject": "Comité (cancelado)"}

    async def test_eliminar_y_responder_invitacion_con_aprobacion(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        borrar = await _llamar(cal, "_delete_event_tool", event_id="evt-00001")
        ok = await _llamar(cal, "_delete_event_tool", event_id="evt-00001", aprobacion_id=await _aprobar(mundo, borrar))
        assert ok["success"] is True and len(_escrituras(mundo, "DELETE")) == 1
        responder = await _llamar(
            cal, "_respond_event_tool", event_id="evt-00001", respuesta="aceptar", comentario="Allí estaré"
        )
        ok = await _llamar(
            cal,
            "_respond_event_tool",
            event_id="evt-00001",
            respuesta="aceptar",
            comentario="Allí estaré",
            aprobacion_id=await _aprobar(mundo, responder),
        )
        assert ok["success"] is True, ok
        metodo, ruta, cuerpo = _escrituras(mundo, "POST")[0]
        assert ruta.endswith("/me/events/evt-00001/accept") and cuerpo["comment"] == "Allí estaré"

    async def test_error_de_graph_queda_fallida_y_no_se_reintenta_sola(self, mundo):
        mundo.graph.respuesta_crear = 400
        _, _, cal, _ = await _plugins(mundo)
        args = {"subject": "X", "start": "2026-11-25T15:00:00", "end": "2026-11-25T16:00:00", "timezone": "UTC"}
        pedido = await _llamar(cal, "_create_event_tool", **args)
        await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "fallida"
        r = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and r.get("codigo") == "fallida", r
        mundo.graph.respuesta_crear = 201
        de_nuevo = await _llamar(cal, "_create_event_tool", **args)
        assert de_nuevo["requiere_aprobacion"] is True
        assert len(_escrituras(mundo, "POST")) == 1

    async def test_timeout_exige_conciliacion_manual_y_luego_permite_un_unico_reintento(self, mundo):
        mundo.graph.lista_falla = True
        task_id, _, cal, _ = await _plugins(mundo)
        args = {"subject": "Y", "start": "2026-11-26T15:00:00", "end": "2026-11-26T16:00:00", "timezone": "UTC"}
        pedido = await _llamar(cal, "_create_event_tool", **args)
        mundo.graph.modo = "timeout_tras_aceptar"
        aprob_id = await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(aprob_id))["envio_estado"] == "desconocida"
        r = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=aprob_id)
        assert r["estado"] == "desconocido", r
        mundo.graph.modo = "202"
        bloqueado = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=aprob_id)
        assert bloqueado["success"] is False and len(_escrituras(mundo, "POST")) == 1
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            sin_decision = await c.post(f"{mundo.backend.base}/api/aprobaciones/{aprob_id}/conciliar", headers=headers)
            assert sin_decision.status_code == 409
            assert sin_decision.json()["detail"]["codigo"] == "conciliacion_manual_requerida"
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{aprob_id}/conciliar",
                headers=headers,
                json={"ejecutada": False},
            )
        assert conc.status_code == 200 and conc.json()["estado"] == "aprobada", conc.text
        ok = await _llamar(cal, "_create_event_tool", **args, aprobacion_id=aprob_id)
        assert ok["success"] is True and len(_escrituras(mundo, "POST")) == 2
        assert "conciliada_no_encontrada" in await mundo.eventos_aprobacion(aprob_id)

    async def test_lease_vencido_no_ejecuta(self, mundo):
        task_id, lease, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_delete_event_tool", event_id="evt-00001")
        aprob_id = await _aprobar(mundo, pedido)
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 s' WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        r = await _llamar(cal, "_delete_event_tool", event_id="evt-00001", aprobacion_id=aprob_id)
        assert r["success"] is False and r["codigo"] == "intento_no_vigente"
        assert len(_escrituras(mundo, "DELETE")) == 1


class TestTeams:
    async def test_mensaje_a_chat_con_miembros_resueltos_por_el_servidor(self, mundo):
        task_id, _, _, teams = await _plugins(mundo)
        chat = "19:chat-directo@thread.v2"
        pedido = await _llamar(teams, "_send_chat_message_tool", chat_id=chat, message="Nos vemos a las 10")
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["contenido_json"]["miembros"] == ["admin@hq.example", "colega@hq.example"]
        assert "colega@hq.example" in fila["vista_previa"] and "Nos vemos a las 10" in fila["vista_previa"]
        aprob_id = await _aprobar(mundo, pedido)
        cambiado = await _llamar(
            teams, "_send_chat_message_tool", chat_id=chat, message="Nos vemos a las 11", aprobacion_id=aprob_id
        )
        assert cambiado["requiere_aprobacion"] is True and len(_escrituras(mundo, "POST")) == 1
        ok = await _llamar(
            teams,
            "_send_chat_message_tool",
            chat_id=chat,
            message="Nos vemos a las 11",
            aprobacion_id=await _aprobar(mundo, cambiado),
        )
        assert ok["success"] is True, ok
        assert _escrituras(mundo, "POST")[-1][2]["body"]["content"] == "Nos vemos a las 11"

    async def test_miembros_del_chat_cambian_entre_aprobar_y_enviar(self, mundo):
        _, _, _, teams = await _plugins(mundo)
        chat = "19:chat-grupo@thread.v2"
        pedido = await _llamar(teams, "_send_chat_message_tool", chat_id=chat, message="Informe listo")
        mundo.graph.miembros = ["admin@hq.example", "colega@hq.example", "intruso@externo.example"]
        aprob_id = await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(aprob_id))["estado"] == "expirada"
        r = await _llamar(
            teams, "_send_chat_message_tool", chat_id=chat, message="Informe listo", aprobacion_id=aprob_id
        )
        assert r["requiere_aprobacion"] is True, r
        assert "intruso@externo.example" in r["borrador"]
        assert not _escrituras(mundo)

    async def test_mensaje_a_persona_usa_el_chat_directo_existente(self, mundo):
        _, _, _, teams = await _plugins(mundo)
        pedido = await _llamar(teams, "_send_to_person_tool", email="colega@hq.example", message="¿Revisaste?")
        assert pedido.get("chat_id") == "19:chat-directo@thread.v2", pedido
        ok = await _llamar(
            teams,
            "_send_to_person_tool",
            email="colega@hq.example",
            message="¿Revisaste?",
            aprobacion_id=await _aprobar(mundo, pedido),
        )
        assert ok["success"] is True and len(_escrituras(mundo, "POST")) == 1


class TestAutorizacion:
    async def test_escrituras_directas_y_tokens_bloqueados(self, mundo):
        task_id, lease, cal, teams = await _plugins(mundo)
        cab = cal._cabeceras_control()
        async with httpx.AsyncClient() as c:
            for metodo, ruta in (
                ("POST", "/me/events"),
                ("PATCH", "/me/events/evt-00001"),
                ("POST", "/me/chats/19:x@thread.v2/messages"),
                ("POST", "/chats"),
            ):
                r = await c.post(
                    f"{mundo.backend.base}/api/internal/control/m365/graph",
                    headers=cab,
                    json={"tarea_id": task_id, "metodo": metodo, "ruta": ruta, "cuerpo_json": {}},
                )
                assert r.status_code in (403, 422), (metodo, ruta, r.status_code)
            token = await c.get(
                f"{mundo.backend.base}/api/internal/control/m365/agent-token",
                params={"user_id": mundo.admin_id},
                headers={k: v for k, v in cab.items() if k != "Content-Type"},
            )
        assert token.status_code == 410
        assert not _escrituras(mundo)

    async def test_otro_propietario_no_aprueba_ni_ejecuta(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_delete_event_tool", event_id="evt-00001")
        assert (await mundo.decidir(pedido["aprobacion_id"], usuario="otra-usuaria")).status_code == 404
        await mundo.decidir(pedido["aprobacion_id"])
        _, _, cal_otra, _ = await _plugins(mundo, usuario_id=mundo.otra_id)
        r = await _llamar(cal_otra, "_delete_event_tool", event_id="evt-00001", aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and len(_escrituras(mundo, "DELETE")) == 1
        headers = await _login_como(mundo.backend.base, "otra-usuaria")
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar",
                headers=headers,
                json={"ejecutada": True},
            )
        assert conc.status_code == 404
