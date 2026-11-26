from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_calendario_teams import POLITICA, _aprobar, _llamar, _plugins
from test_e2e_conciliacion_graph import YO, _ahora, _GraphConEstado
from test_e2e_correo_central import _Mundo

pytestmark = [pytest.mark.integration, requires_database]

CHAT = "19:chat-directo@thread.v2"
NUEVA = {
    "id": "user-nueva",
    "displayName": "Nueva Persona",
    "mail": "nueva@hq.example",
    "userPrincipalName": "nueva@hq.example",
}


class _GraphTeams(_GraphConEstado):
    chats: dict = {}
    subidas: dict = {}
    chat_403 = False

    def do_GET(self):
        ruta = self._ruta()
        cls = type(self)
        if ruta == "/users":
            self._registrar()
            return self._json(200, {"value": [NUEVA] if "nueva" in self.path else []})
        if m := re.match(r"^/users/([^/]+)$", ruta):
            self._registrar()
            return self._json(200, NUEVA) if m.group(1) == NUEVA["id"] else self._json(404, {})
        if ruta == "/me/chats":
            self._registrar()
            return self._json(
                200,
                {
                    "value": [
                        {"id": cid, "chatType": "oneOnOne", "members": c["members"]} for cid, c in cls.chats.items()
                    ]
                },
            )
        if ruta == "/me" and "userPrincipalName" in self.path:
            self._registrar()
            return self._json(200, {"id": YO, "mail": "admin@hq.example", "userPrincipalName": "admin@hq.example"})
        if (m := re.match(r"^/me/chats/([^/]+)$", ruta)) and m.group(1) in cls.chats:
            return self._json(
                200, {"id": m.group(1), "chatType": "oneOnOne", "members": cls.chats[m.group(1)]["members"]}
            )
        if m := re.match(r"^/me/chats/([^/]+)/messages$", ruta):
            if m.group(1) in cls.chats:
                self._registrar()
                return self._json(200, {"value": list(reversed(cls.chats[m.group(1)]["mensajes"]))})
        return super().do_GET()

    def do_POST(self):
        ruta = self._ruta()
        cls = type(self)
        if ruta == "/chats":
            cuerpo = self._cuerpo()
            self._registrar(cuerpo)
            cls.escrituras.append(("POST", ruta, cuerpo))
            if cls.chat_403:
                return self._json(403, {"error": {"code": "Forbidden"}})
            ids = sorted(re.search(r"\('([^']+)'\)", m["user@odata.bind"]).group(1) for m in cuerpo["members"])
            for cid, c in cls.chats.items():
                if sorted(x["userId"] for x in c["members"]) == ids:
                    return self._json(200, {"id": cid})
            cid = f"19:nuevo-{len(cls.chats) + 1}@unq.gbl.spaces"
            cls.chats[cid] = {"members": [{"userId": i} for i in ids], "mensajes": []}
            return self._json(201, {"id": cid})
        if m := re.match(r"^/me/chats/([^/]+)/messages$", ruta):
            if m.group(1) in cls.chats:

                def enviar(cuerpo):
                    mensaje = {
                        "id": f"msg-{len(cls.escrituras)}",
                        "createdDateTime": _ahora(),
                        "from": {"user": {"id": YO}},
                        "body": cuerpo.get("body"),
                        "attachments": cuerpo.get("attachments") or [],
                    }
                    cls.chats[m.group(1)]["mensajes"].append(mensaje)
                    cls.mensajes.append(mensaje)
                    return 201, mensaje

                return self._aplicar(enviar)
        return super().do_POST()

    def do_PUT(self):
        ruta = self._ruta()
        largo = int(self.headers.get("Content-Length") or 0)
        datos = self.rfile.read(largo)
        self._registrar({"bytes": len(datos)})
        m = re.match(r"^/me/drive/root:/(.+):/content$", ruta)
        if not m:
            return self._json(404, {})
        import urllib.parse

        destino = urllib.parse.unquote(m.group(1))
        type(self).subidas[destino] = datos
        nombre = destino.rsplit("/", 1)[-1]
        return self._json(
            201,
            {
                "id": f"item-{hashlib.sha256(destino.encode()).hexdigest()[:10]}",
                "name": nombre,
                "webUrl": f"https://contoso.sharepoint.example/{nombre}",
            },
        )


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphTeams",
        (_GraphTeams,),
        {
            "eventos": {},
            "mensajes": [],
            "escrituras": [],
            "caidas": set(),
            "subidas": {},
            "chat_403": False,
            "chats": {CHAT: {"members": [{"userId": YO}, {"userId": "user-colega"}], "mensajes": []}},
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


def _archivo(m, nombre: str, datos: bytes) -> bytes:
    (m.workspace / nombre).write_bytes(datos)
    return datos


def _creaciones_de_chat(m) -> list:
    return [e for e in m.graph.escrituras if e[1] == "/chats"]


class TestArchivosEnTeams:
    async def test_archivo_aprobado_por_huella_se_sube_y_se_adjunta(self, mundo):
        datos = _archivo(mundo, "acta.pdf", b"%PDF acta sintetica " * 500)
        _, _, _, teams = await _plugins(mundo)
        args = {"chat_id": CHAT, "message": "Te dejo el acta", "file_path": "acta.pdf"}
        pedido = await _llamar(teams, "_send_chat_message_tool", **args)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["contenido_json"]["archivo"] == {
            "nombre": "acta.pdf",
            "sha256": hashlib.sha256(datos).hexdigest(),
            "bytes": len(datos),
        }
        assert "acta.pdf" in fila["vista_previa"] and not mundo.graph.subidas
        r = await _llamar(teams, "_send_chat_message_tool", **args, aprobacion_id=await _aprobar(mundo, pedido))
        assert r["success"] is True, r
        ((destino, subido),) = mundo.graph.subidas.items()
        assert subido == datos and destino.endswith(f"{pedido['aprobacion_id'][:8]}_acta.pdf")
        (mensaje,) = mundo.graph.chats[CHAT]["mensajes"]
        assert "Te dejo el acta" in mensaje["body"]["content"]
        assert mensaje["attachments"][0]["contentType"] == "reference"
        assert mensaje["attachments"][0]["name"].endswith("acta.pdf")

    async def test_archivo_cambiado_tras_aprobar_pide_aprobacion_nueva_sin_subir(self, mundo):
        _archivo(mundo, "acta.pdf", b"version 1")
        _, _, _, teams = await _plugins(mundo)
        args = {"chat_id": CHAT, "message": "Acta", "file_path": "acta.pdf"}
        pedido = await _llamar(teams, "_send_chat_message_tool", **args)
        aprob_id = await _aprobar(mundo, pedido)
        _archivo(mundo, "acta.pdf", b"version 2 alterada")
        r = await _llamar(teams, "_send_chat_message_tool", **args, aprobacion_id=aprob_id)
        assert r.get("requiere_aprobacion") is True and r["aprobacion_id"] != aprob_id
        assert not mundo.graph.subidas and not mundo.graph.chats[CHAT]["mensajes"]

    async def test_bytes_distintos_a_los_aprobados_se_rechazan_en_el_backend(self, mundo):
        _archivo(mundo, "acta.pdf", b"original aprobado")
        task_id, _, _, teams = await _plugins(mundo)
        args = {"chat_id": CHAT, "message": "Acta", "file_path": "acta.pdf"}
        pedido = await _llamar(teams, "_send_chat_message_tool", **args)
        aprob_id = await _aprobar(mundo, pedido)
        fila = await mundo.aprobacion(aprob_id)
        contenido = {
            "metodo": "POST",
            "ruta": f"/me/chats/{CHAT}/messages",
            "cuerpo": fila["contenido_json"]["cuerpo"],
            "archivo": fila["contenido_json"]["archivo"],
        }
        r = teams._llamar_control(
            "POST",
            "/control/m365/accion/ejecutar",
            {
                "tarea_id": task_id,
                "aprobacion_id": aprob_id,
                "contenido": contenido,
                "archivo_b64": base64.b64encode(b"otra cosa").decode(),
            },
        )
        assert r["ok"] is False and r["codigo"] == "adjunto_distinto_de_lo_aprobado", r
        assert not mundo.graph.subidas and (await mundo.aprobacion(aprob_id))["estado"] == "aprobada"

    async def test_archivo_fuera_del_espacio_o_grande_no_se_propone(self, mundo, tmp_path):
        (tmp_path / "secreto.txt").write_text("no")
        (mundo.workspace / "enorme.bin").write_bytes(b"0" * (4 * 1024 * 1024 + 1))
        _, _, _, teams = await _plugins(mundo)
        fuera = await _llamar(
            teams, "_send_chat_message_tool", chat_id=CHAT, message="x", file_path=str(tmp_path / "secreto.txt")
        )
        grande = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="x", file_path="enorme.bin")
        assert "fuera del directorio" in fuera["error"] and "límite" in grande["error"]
        async with mundo.maker() as s:
            from sqlalchemy import text

            assert (await s.execute(text("SELECT count(*) FROM approvals"))).scalar_one() == 0


class TestChatNuevo:
    async def test_escribir_a_persona_sin_chat_lo_abre_una_vez_con_aprobacion(self, mundo):
        _, _, _, teams = await _plugins(mundo)
        args = {"email": "nueva@hq.example", "message": "Hola, soy del equipo de finanzas"}
        pedido = await _llamar(teams, "_send_to_person_tool", **args)
        assert pedido.get("requiere_aprobacion") is True, pedido
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["contenido_json"]["ruta"] == "/me/chats/directo:user-nueva/messages"
        assert fila["contenido_json"]["miembros"] == ["admin@hq.example", "nueva@hq.example"]
        assert "abre el chat directo" in fila["vista_previa"] and not _creaciones_de_chat(mundo)
        r = await _llamar(teams, "_send_to_person_tool", **args, aprobacion_id=await _aprobar(mundo, pedido))
        assert r["success"] is True, r
        assert len(_creaciones_de_chat(mundo)) == 1
        nuevo = [c for c in mundo.graph.chats if c != CHAT]
        assert len(nuevo) == 1 and mundo.graph.chats[nuevo[0]]["mensajes"][0]["body"]["content"] == args["message"]

        _, _, _, teams2 = await _plugins(mundo)
        segundo = await _llamar(teams2, "_send_to_person_tool", email="nueva@hq.example", message="Otro mensaje")
        assert (await mundo.aprobacion(segundo["aprobacion_id"]))["contenido_json"]["ruta"] == (
            f"/me/chats/{nuevo[0]}/messages"
        )
        ok = await _llamar(
            teams2,
            "_send_to_person_tool",
            email="nueva@hq.example",
            message="Otro mensaje",
            aprobacion_id=await _aprobar(mundo, segundo),
        )
        assert ok["success"] is True and len(_creaciones_de_chat(mundo)) == 1
        assert [m["body"]["content"] for m in mundo.graph.chats[nuevo[0]]["mensajes"]] == [
            args["message"],
            "Otro mensaje",
        ]

    async def test_timeout_tras_abrir_chat_y_enviar_no_duplica(self, mundo):
        task_id, _, _, teams = await _plugins(mundo)
        args = {"email": "nueva@hq.example", "message": "Mensaje único"}
        pedido = await _llamar(teams, "_send_to_person_tool", **args)
        mundo.graph.modo = "timeout_tras_aceptar"
        aprob_id = await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(aprob_id))["envio_estado"] == "desconocida"
        mundo.graph.modo = "202"
        r = await _llamar(teams, "_send_to_person_tool", **args, aprobacion_id=aprob_id)
        assert r["success"] is True, r
        nuevo = [c for c in mundo.graph.chats if c != CHAT]
        assert len(mundo.graph.chats[nuevo[0]]["mensajes"]) == 1
        assert "conciliada_confirmada" in await mundo.eventos_aprobacion(aprob_id)

    async def test_sin_permiso_para_crear_chat_falla_sin_enviar(self, mundo):
        mundo.graph.chat_403 = True
        task_id, _, _, teams = await _plugins(mundo)
        args = {"email": "nueva@hq.example", "message": "Hola"}
        pedido = await _llamar(teams, "_send_to_person_tool", **args)
        await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "fallida"
        r = await _llamar(teams, "_send_to_person_tool", **args, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False, r
        assert "fallida" in await mundo.eventos_aprobacion(pedido["aprobacion_id"])
        assert not mundo.graph.mensajes
        assert json.dumps(r).find("chat_no_creado") >= 0
