from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_calendario_teams import POLITICA, _aprobar, _llamar, _plugins
from test_e2e_correo_central import _Graph, _login, _Mundo

pytestmark = [pytest.mark.integration, requires_database]

CHAT = "19:chat-directo@thread.v2"
YO = "user-admin"


def _ahora(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


class _GraphConEstado(_Graph):
    eventos: dict = {}
    mensajes: list = []
    escrituras: list = []
    caidas: set = set()

    def _json(self, estado, datos=None):
        if datos is None:
            self.send_response(estado)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cuerpo = json.dumps(datos).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cuerpo(self):
        largo = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(largo) or b"{}") if largo else {}

    def _ruta(self):
        return self.path.split("?", 1)[0].replace("/v1", "", 1)

    def do_GET(self):
        ruta = self._ruta()
        self._registrar()
        cls = type(self)
        if any(re.search(c, ruta) for c in cls.caidas):
            return self._json(503, {"error": {"code": "serviceNotAvailable"}})
        if ruta == "/me":
            return self._json(200, {"id": YO, "mail": "admin@hq.example"})
        if ruta == "/me/events":
            return self._json(200, {"value": list(cls.eventos.values())})
        if m := re.match(r"^/me/events/([^/]+)$", ruta):
            evento = cls.eventos.get(m.group(1))
            return self._json(200, evento) if evento else self._json(404, {"error": {"code": "ErrorItemNotFound"}})
        if re.match(r"^/me/chats/[^/]+/members$", ruta):
            return self._json(200, {"value": [{"email": "admin@hq.example"}, {"email": "colega@hq.example"}]})
        if m := re.match(r"^/me/chats/([^/]+)$", ruta):
            miembros = [
                {"email": "admin@hq.example", "displayName": "Admin"},
                {"email": "colega@hq.example", "displayName": "Colega"},
            ]
            return self._json(200, {"id": m.group(1), "chatType": "oneOnOne", "topic": None, "members": miembros})
        if re.match(r"^/me/chats/[^/]+/messages$", ruta):
            return self._json(200, {"value": list(reversed(cls.mensajes))})
        return self._json(404, {"error": {}})

    def _aplicar(self, accion):
        cls = type(self)
        cuerpo = self._cuerpo()
        self._registrar(cuerpo)
        cls.escrituras.append((self.command, self._ruta(), cuerpo))
        if cls.modo == "falla_antes":
            self.connection.close()
            return None
        estado, datos = accion(cuerpo)
        if cls.modo == "timeout_tras_aceptar":
            self.connection.close()
            return None
        return self._json(estado, datos)

    def do_POST(self):
        ruta = self._ruta()
        cls = type(self)
        if ruta == "/me/events":

            def crear(cuerpo):
                for evento in cls.eventos.values():
                    if cuerpo.get("transactionId") and evento.get("transactionId") == cuerpo["transactionId"]:
                        return 201, evento
                eid = f"evt-{len(cls.eventos) + 1:05d}"
                cls.eventos[eid] = {
                    **cuerpo,
                    "id": eid,
                    "createdDateTime": _ahora(),
                    "responseStatus": {"response": "organizer"},
                }
                return 201, cls.eventos[eid]

            return self._aplicar(crear)
        if m := re.match(r"^/me/events/([^/]+)/(accept|decline|tentativelyAccept)$", ruta):

            def responder(_cuerpo):
                cls.eventos[m.group(1)]["responseStatus"] = {
                    "response": {"accept": "accepted", "decline": "declined"}.get(m.group(2), "tentativelyAccepted")
                }
                return 202, None

            return self._aplicar(responder)
        if re.match(r"^/me/chats/[^/]+/messages$", ruta):

            def enviar(cuerpo):
                mensaje = {
                    "id": f"msg-{len(cls.mensajes) + 1}",
                    "createdDateTime": _ahora(),
                    "from": {"user": {"id": YO}},
                    "body": cuerpo.get("body"),
                }
                cls.mensajes.append(mensaje)
                return 201, mensaje

            return self._aplicar(enviar)
        return self._json(404, {})

    def do_PATCH(self):
        m = re.match(r"^/me/events/([^/]+)$", self._ruta())

        def modificar(cuerpo):
            type(self).eventos[m.group(1)].update(cuerpo)
            return 200, type(self).eventos[m.group(1)]

        return self._aplicar(modificar)

    def do_DELETE(self):
        m = re.match(r"^/me/events/([^/]+)$", self._ruta())

        def eliminar(_cuerpo):
            type(self).eventos.pop(m.group(1), None)
            return 204, None

        return self._aplicar(eliminar)


EVENTO_BASE = {
    "id": "evt-00001",
    "subject": "Comité",
    "start": {"dateTime": "2026-11-20T10:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-20T11:00:00.0000000", "timeZone": "UTC"},
    "createdDateTime": "2026-10-01T10:00:00Z",
    "responseStatus": {"response": "notResponded"},
    "attendees": [],
}


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphConEstado",
        (_GraphConEstado,),
        {
            "eventos": {"evt-00001": json.loads(json.dumps(EVENTO_BASE))},
            "mensajes": [],
            "escrituras": [],
            "caidas": set(),
        },
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


def _escrituras(m, metodo=None, patron=""):
    return [e for e in m.graph.escrituras if (metodo is None or e[0] == metodo) and re.search(patron, e[1])]


ARGS_EVENTO = {"subject": "Revisión", "start": "2026-11-25T15:00:00", "end": "2026-11-25T16:00:00", "timezone": "UTC"}


async def _incierto(m, plugin, herramienta, **args):
    pedido = await _llamar(plugin, herramienta, **args)
    m.graph.modo = "timeout_tras_aceptar"
    aprob_id = await _aprobar(m, pedido)
    fila = await m.aprobacion(aprob_id)
    assert (fila["estado"], fila["envio_estado"]) == ("ejecutando", "desconocida"), dict(fila)
    m.graph.modo = "202"
    return aprob_id


class TestCalendario:
    async def test_crear_aplicado_antes_del_timeout_no_duplica_la_reunion(self, mundo):
        task_id, _, cal, _ = await _plugins(mundo)
        aprob_id = await _incierto(mundo, cal, "_create_event_tool", **ARGS_EVENTO)
        r = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO, aprobacion_id=aprob_id)
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "POST", r"^/me/events$")) == 1
        creadas = [e for e in mundo.graph.eventos.values() if e.get("subject") == "Revisión"]
        assert len(creadas) == 1 and creadas[0]["transactionId"] == f"hermeshq-{aprob_id}"
        fila = await mundo.aprobacion(aprob_id)
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutada", "confirmada")
        assert fila["envio_proveedor_id"] == creadas[0]["id"]
        assert "conciliada_confirmada" in await mundo.eventos_aprobacion(aprob_id)
        otra = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO, aprobacion_id=aprob_id)
        assert otra["success"] is True and otra.get("ya_ejecutada"), otra
        assert len(_escrituras(mundo, "POST", r"^/me/events$")) == 1

    async def test_crear_que_no_llego_a_graph_se_ejecuta_una_sola_vez(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO)
        mundo.graph.modo = "falla_antes"
        aprob_id = await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(aprob_id))["envio_estado"] == "desconocida"
        mundo.graph.modo = "202"
        r = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO, aprobacion_id=aprob_id)
        assert r["success"] is True, r
        assert len([e for e in mundo.graph.eventos.values() if e.get("subject") == "Revisión"]) == 1
        assert "conciliada_no_encontrada" in await mundo.eventos_aprobacion(aprob_id)

    async def test_graph_descarta_el_post_repetido_por_transaction_id(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        aprob_id = await _incierto(mundo, cal, "_create_event_tool", **ARGS_EVENTO)
        mundo.graph.caidas.add(r"^/me/events$")
        bloqueado = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO, aprobacion_id=aprob_id)
        assert bloqueado["codigo"] == "conciliacion_manual_requerida", bloqueado
        assert len(_escrituras(mundo, "POST", r"^/me/events$")) == 1
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{aprob_id}/conciliar",
                headers=headers,
                json={"ejecutada": False},
            )
        assert r.status_code == 200 and r.json()["estado"] == "aprobada"
        mundo.graph.caidas.clear()
        ok = await _llamar(cal, "_create_event_tool", **ARGS_EVENTO, aprobacion_id=aprob_id)
        assert ok["success"] is True
        assert len(_escrituras(mundo, "POST", r"^/me/events$")) == 2
        assert len([e for e in mundo.graph.eventos.values() if e.get("subject") == "Revisión"]) == 1

    async def test_modificar_aplicado_se_confirma_sin_repetir(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        aprob_id = await _incierto(mundo, cal, "_update_event_tool", event_id="evt-00001", subject="Comité (movido)")
        r = await _llamar(
            cal, "_update_event_tool", event_id="evt-00001", subject="Comité (movido)", aprobacion_id=aprob_id
        )
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "PATCH")) == 1
        assert (await mundo.aprobacion(aprob_id))["envio_estado"] == "confirmada"

    async def test_modificar_no_aplicado_se_ejecuta_una_vez(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_update_event_tool", event_id="evt-00001", subject="Comité (nuevo)")
        mundo.graph.modo = "falla_antes"
        aprob_id = await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(aprob_id))["envio_estado"] == "desconocida"
        mundo.graph.modo = "202"
        r = await _llamar(
            cal, "_update_event_tool", event_id="evt-00001", subject="Comité (nuevo)", aprobacion_id=aprob_id
        )
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "PATCH")) == 2
        assert mundo.graph.eventos["evt-00001"]["subject"] == "Comité (nuevo)"

    async def test_eliminar_aplicado_se_confirma_por_404(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        aprob_id = await _incierto(mundo, cal, "_delete_event_tool", event_id="evt-00001")
        r = await _llamar(cal, "_delete_event_tool", event_id="evt-00001", aprobacion_id=aprob_id)
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "DELETE")) == 1

    async def test_aceptar_invitacion_aplicada_no_se_repite(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        args = {"event_id": "evt-00001", "respuesta": "aceptar", "comentario": "Ok"}
        aprob_id = await _incierto(mundo, cal, "_respond_event_tool", **args)
        r = await _llamar(cal, "_respond_event_tool", **args, aprobacion_id=aprob_id)
        assert r["success"] is True, r
        assert len(_escrituras(mundo, "POST", r"/accept$")) == 1


class TestTeams:
    async def test_mensaje_aplicado_antes_del_timeout_no_se_duplica(self, mundo):
        task_id, _, _, teams = await _plugins(mundo)
        aprob_id = await _incierto(mundo, teams, "_send_chat_message_tool", chat_id=CHAT, message="Nos vemos a las 10")
        r = await _llamar(
            teams, "_send_chat_message_tool", chat_id=CHAT, message="Nos vemos a las 10", aprobacion_id=aprob_id
        )
        assert r["success"] is True, r
        assert len(mundo.graph.mensajes) == 1
        fila = await mundo.aprobacion(aprob_id)
        assert (fila["estado"], fila["envio_estado"], fila["envio_proveedor_id"]) == (
            "ejecutada",
            "confirmada",
            "msg-1",
        )
        assert "conciliada_confirmada" in await mundo.eventos_aprobacion(aprob_id)

    async def test_mensaje_que_no_salio_se_envia_una_vez(self, mundo):
        _, _, _, teams = await _plugins(mundo)
        pedido = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Hola equipo")
        aprob_id = await _aprobar(mundo, pedido)
        mundo.graph.modo = "falla_antes"
        await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Hola equipo", aprobacion_id=aprob_id)
        mundo.graph.modo = "202"
        r = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Hola equipo", aprobacion_id=aprob_id)
        assert r["success"] is True, r
        assert len(mundo.graph.mensajes) == 1

    async def test_mismo_texto_anterior_o_de_otra_persona_no_cuenta_como_enviado(self, mundo):
        mundo.graph.mensajes.extend(
            [
                {
                    "id": "viejo",
                    "createdDateTime": _ahora(-timedelta(hours=2)),
                    "from": {"user": {"id": YO}},
                    "body": {"content": "Listo"},
                },
            ]
        )
        _, _, _, teams = await _plugins(mundo)
        pedido = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Listo")
        mundo.graph.modo = "falla_antes"
        aprob_id = await _aprobar(mundo, pedido)
        mundo.graph.mensajes.append(
            {
                "id": "ajeno",
                "createdDateTime": _ahora(),
                "from": {"user": {"id": "user-colega"}},
                "body": {"content": "Listo"},
            }
        )
        mundo.graph.modo = "202"
        r = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Listo", aprobacion_id=aprob_id)
        assert r["success"] is True, r
        propios = [m for m in mundo.graph.mensajes if m["from"]["user"]["id"] == YO]
        assert len(propios) == 2 and propios[0]["id"] == "viejo", [m["id"] for m in propios]

    async def test_sin_poder_consultar_queda_manual_y_no_reenvia(self, mundo):
        _, _, _, teams = await _plugins(mundo)
        aprob_id = await _incierto(mundo, teams, "_send_chat_message_tool", chat_id=CHAT, message="Urgente")
        mundo.graph.caidas.add(r"/messages$")
        r = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="Urgente", aprobacion_id=aprob_id)
        assert r["success"] is False and r["codigo"] == "conciliacion_manual_requerida", r
        assert len(_escrituras(mundo, "POST", r"/messages$")) == 1
        fila = await mundo.aprobacion(aprob_id)
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutando", "desconocida")

    async def test_conciliar_desde_la_interfaz_sin_decision_verifica_en_teams(self, mundo):
        task_id, _, _, teams = await _plugins(mundo)
        aprob_id = await _incierto(mundo, teams, "_send_chat_message_tool", chat_id=CHAT, message="Acta enviada")
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{mundo.backend.base}/api/aprobaciones/{aprob_id}/conciliar", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["envio_estado"] == "confirmada" and r.json()["encontrado"] is True
        assert len(mundo.graph.mensajes) == 1
        assert "conciliada_confirmada" in await mundo.eventos_aprobacion(aprob_id)
