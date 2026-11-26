from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_correo_central import _Mundo
from test_e2e_correo_migrado import _GraphCorreo

pytestmark = [pytest.mark.integration, requires_database]

MB = 1_000_000
TROZO = 327_680 * 10


class _GraphGrande(_GraphCorreo):
    sesiones: dict = {}
    host_subida: str | None = None
    fallar_trozo: int | None = None
    borrador_403 = False
    eliminados: list = []

    def _leer(self):
        largo = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(largo)

    def do_POST(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        cls = type(self)
        if ruta == "/me/messages":
            cuerpo = json.loads(self._leer() or b"{}")
            self._registrar(cuerpo)
            if cls.borrador_403:
                return self._json(403, {"error": {"code": "ErrorAccessDenied"}})
            bid = f"borrador-{len(cls.borradores) + 1}"
            cls.borradores[bid] = {"accion": "nuevo", "inicial": cuerpo, **cuerpo, "adjuntos": []}
            return self._json(201, {"id": bid})
        if m := re.match(r"^/me/messages/(borrador-\d+)/attachments/createUploadSession$", ruta):
            cuerpo = json.loads(self._leer() or b"{}")
            self._registrar(cuerpo)
            sid = uuid.uuid4().hex[:8]
            item = cuerpo["AttachmentItem"]
            cls.sesiones[sid] = {"borrador": m.group(1), "item": item, "datos": bytearray(), "trozos": 0}
            puerto = self.server.server_address[1]
            base = cls.host_subida or f"http://127.0.0.1:{puerto}"
            return self._json(201, {"uploadUrl": f"{base}/subida/{sid}"})
        return super().do_POST()

    def do_PUT(self):
        ruta = self.path.split("?", 1)[0]
        cls = type(self)
        datos = self._leer()
        cls.peticiones.append(
            {
                "metodo": "PUT",
                "ruta": ruta,
                "auth": self.headers.get("Authorization"),
                "cuerpo": len(datos),
                "rango": self.headers.get("Content-Range"),
            }
        )
        m = re.match(r"^/subida/([0-9a-f]+)$", ruta)
        if not m or m.group(1) not in cls.sesiones:
            return self._json(404, {})
        sesion = cls.sesiones[m.group(1)]
        sesion["trozos"] += 1
        if cls.fallar_trozo is not None and sesion["trozos"] == cls.fallar_trozo:
            return self._json(500, {"error": {"code": "serviceNotAvailable"}})
        inicio, resto = self.headers["Content-Range"].split(" ", 1)[1].split("-", 1)
        assert int(inicio) == len(sesion["datos"])
        sesion["datos"].extend(datos)
        total = int(resto.split("/", 1)[1])
        if len(sesion["datos"]) == total:
            cls.borradores[sesion["borrador"]]["adjuntos"].append(
                {
                    "name": sesion["item"]["name"],
                    "sha256": hashlib.sha256(bytes(sesion["datos"])).hexdigest(),
                    "bytes": total,
                    "sesion": True,
                }
            )
            return self._json(201, {"id": "adj"})
        return self._json(200, {"nextExpectedRanges": [f"{len(sesion['datos'])}-"]})

    def do_DELETE(self):
        self._registrar()
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        type(self).eliminados.append(ruta.rsplit("/", 1)[-1])
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if "/mailFolders/sentitems/messages" in self.path:
            self._registrar()
            valor = []
            for i, m in enumerate(type(self).enviados):
                cabeceras = m.get("internetMessageHeaders") or (m.get("inicial") or {}).get("internetMessageHeaders")
                valor.append({"id": f"enviado-{i}", "internetMessageHeaders": cabeceras or []})
            return self._json(200, {"value": valor})
        return super().do_GET()


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphGrande",
        (_GraphGrande,),
        {
            "borradores": {},
            "marcados": [],
            "sesiones": {},
            "eliminados": [],
            "host_subida": None,
            "fallar_trozo": None,
            "borrador_403": False,
        },
    )
    m = _Mundo(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        graph_cls=graph,
    )
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


def _archivo(m: _Mundo, nombre: str, tam: int) -> bytes:
    datos = (hashlib.sha256(nombre.encode()).digest() * (tam // 32 + 1))[:tam]
    (m.workspace / nombre).write_bytes(datos)
    return datos


def _peticiones(m: _Mundo, patron: str, metodo: str | None = None) -> list[dict]:
    return [p for p in m.graph.peticiones if re.search(patron, p["ruta"]) and (metodo is None or p["metodo"] == metodo)]


async def _aprobado(m: _Mundo, adjuntos: list[str]):
    task_id, lease = await m.tarea()
    plugin = m.plugin_para(task_id, lease)
    pedido = await asyncio.to_thread(m.enviar, plugin, attachments=[{"path": a} for a in adjuntos])
    assert pedido.get("requiere_aprobacion") is True, pedido
    assert (await m.decidir(pedido["aprobacion_id"])).status_code == 200
    return task_id, lease, plugin, pedido


async def _enviar(m, plugin, pedido, adjuntos):
    return await asyncio.to_thread(
        m.enviar, plugin, aprobacion_id=pedido["aprobacion_id"], attachments=[{"path": a} for a in adjuntos]
    )


class TestLimiteOriginal:
    async def test_hasta_2_8_mb_va_directo_por_sendmail(self, mundo):
        datos = _archivo(mundo, "chico.pdf", 2 * MB)
        task_id, _, plugin, pedido = await _aprobado(mundo, ["chico.pdf"])
        r = await _enviar(mundo, plugin, pedido, ["chico.pdf"])
        assert r["success"] is True, r
        assert len(_peticiones(mundo, r"/me/sendMail$")) == 1
        assert not mundo.graph.sesiones and not mundo.graph.borradores
        import base64

        adj = {a["name"]: base64.b64decode(a["contentBytes"]) for a in mundo.graph.enviados[0]["attachments"]}
        assert adj["chico.pdf"] == datos

    async def test_12_mb_por_borrador_y_sesion_de_carga_con_lo_aprobado(self, mundo):
        datos = _archivo(mundo, "grande.pdf", 12 * MB)
        task_id, lease, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        aprobado = {a["nombre"]: a for a in fila["contenido_json"]["adjuntos"]}
        assert aprobado["grande.pdf"]["sha256"] == hashlib.sha256(datos).hexdigest()
        assert aprobado["grande.pdf"]["bytes"] == 12 * MB

        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is True and r["entregado"] is False, r
        assert not _peticiones(mundo, r"/me/sendMail$")
        assert len(_peticiones(mundo, r"/borrador-1/send$")) == 1
        (sesion,) = mundo.graph.sesiones.values()
        assert sesion["trozos"] == -(-12 * MB // TROZO)
        puts = _peticiones(mundo, r"^/subida/", "PUT")
        assert all(p["auth"] is None for p in puts), "la URL de carga es preautorizada: no debe llevar el token"
        assert max(p["cuerpo"] for p in puts) == TROZO
        borrador = mundo.graph.borradores["borrador-1"]
        grandes = [a for a in borrador["adjuntos"] if a.get("sesion")]
        assert grandes == [
            {"name": "grande.pdf", "sha256": hashlib.sha256(datos).hexdigest(), "bytes": 12 * MB, "sesion": True}
        ]
        assert {a.get("name") for a in borrador["adjuntos"]} >= {"grande.pdf", "informe.txt"} - {"informe.txt"}
        assert borrador["body"]["content"] == fila["contenido_json"]["cuerpo_html"]
        assert {"name": "x-hermeshq-aprobacion", "value": pedido["aprobacion_id"]} in borrador["internetMessageHeaders"]
        final = await mundo.aprobacion(pedido["aprobacion_id"])
        assert (final["estado"], final["envio_estado"]) == ("ejecutada", "aceptada_proveedor")
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["confirmada"]

    async def test_varios_archivos_hasta_25_mb(self, mundo):
        nombres = ["a.xlsx", "b.pptx", "c.docx"]
        tam = {"a.xlsx": 9 * MB, "b.pptx": 9 * MB, "c.docx": 1 * MB}
        hashes = {n: hashlib.sha256(_archivo(mundo, n, tam[n])).hexdigest() for n in nombres}
        _, _, plugin, pedido = await _aprobado(mundo, nombres)
        r = await _enviar(mundo, plugin, pedido, nombres)
        assert r["success"] is True, r
        adjuntos = mundo.graph.borradores["borrador-1"]["adjuntos"]
        por_sesion = {a["name"]: a["sha256"] for a in adjuntos if a.get("sesion")}
        assert por_sesion == {"a.xlsx": hashes["a.xlsx"], "b.pptx": hashes["b.pptx"]}
        assert "c.docx" in {a.get("name") for a in adjuntos if not a.get("sesion")}

    async def test_mas_de_25_mb_se_rechaza_antes_de_pedir_aprobacion(self, mundo):
        _archivo(mundo, "enorme.zip", 26 * MB)
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        r = await asyncio.to_thread(mundo.enviar, plugin, attachments=[{"path": "enorme.zip"}])
        assert r["success"] is False and "25 MB" in r["error"]
        async with mundo.maker() as s:
            assert (await s.execute(text("SELECT count(*) FROM approvals"))).scalar_one() == 0
        assert not mundo.graph.peticiones


class TestGarantiasConservadas:
    async def test_archivo_cambiado_tras_aprobar_no_sube_nada(self, mundo):
        _archivo(mundo, "grande.pdf", 6 * MB)
        _, _, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        _archivo(mundo, "grande.pdf", 6 * MB + 1)
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is False and r.get("requiere_aprobacion") is True
        assert not mundo.graph.borradores and not mundo.graph.sesiones

    async def test_fallo_de_un_trozo_es_fallida_sin_envio_y_borra_el_borrador(self, mundo):
        _archivo(mundo, "grande.pdf", 8 * MB)
        task_id, _, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        mundo.graph.fallar_trozo = 2
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is False and r["ejecutado"] is False, r
        assert not _peticiones(mundo, r"/send$")
        assert mundo.graph.eliminados == ["borrador-1"]
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert (fila["estado"], fila["envio_estado"]) == ("fallida", "fallida")
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["no_ejecutada"]
        mundo.graph.fallar_trozo = None
        otra = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert otra["success"] is False and otra.get("requiere_aprobacion") is True
        assert otra["aprobacion_id"] != pedido["aprobacion_id"]
        assert not _peticiones(mundo, r"/send$")

    async def test_url_de_carga_fuera_de_microsoft_no_recibe_bytes(self, mundo):
        _archivo(mundo, "grande.pdf", 5 * MB)
        _, _, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        mundo.graph.host_subida = "http://atacante.invalid:9"
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is False
        assert "host_no_permitido" in r["error"]
        assert not _peticiones(mundo, r"^/subida/", "PUT")
        assert mundo.graph.eliminados == ["borrador-1"]
        assert not _peticiones(mundo, r"/send$")

    async def test_timeout_al_enviar_queda_incierto_no_se_repite_y_se_concilia(self, mundo):
        _archivo(mundo, "grande.pdf", 4 * MB)
        task_id, lease, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        mundo.graph.modo = "timeout_tras_aceptar"
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is False and r["estado_envio"] == "desconocida", r
        mundo.graph.modo = "202"
        r2 = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r2["success"] is False
        assert len(_peticiones(mundo, r"/send$")) == 1
        mundo.graph.enviados[:] = [dict(mundo.graph.borradores["borrador-1"])]
        import httpx
        from test_e2e_correo_central import _login_como

        cab = await _login_como(mundo.backend.base, "admin")
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=cab
            )
        assert conc.status_code == 200, conc.text
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["envio_estado"] == "confirmada"

    async def test_intento_obsoleto_no_crea_borrador_ni_sube(self, mundo):
        _archivo(mundo, "grande.pdf", 5 * MB)
        _, lease, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET state = 'abandoned', lease_generation = lease_generation + 1 "
                    "WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["codigo"] == "intento_no_vigente"
        assert not mundo.graph.borradores and not mundo.graph.sesiones

    async def test_envios_concurrentes_suben_y_envian_una_sola_vez(self, mundo):
        _archivo(mundo, "grande.pdf", 4 * MB)
        _, _, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        resultados = await asyncio.gather(*(_enviar(mundo, plugin, pedido, ["grande.pdf"]) for _ in range(3)))
        assert sum(1 for r in resultados if r["success"]) == 1, resultados
        assert len(mundo.graph.sesiones) == 1
        assert len(_peticiones(mundo, r"/send$")) == 1

    async def test_sin_permiso_de_borradores_lo_explica_sin_enviar(self, mundo):
        _archivo(mundo, "grande.pdf", 4 * MB)
        _, _, plugin, pedido = await _aprobado(mundo, ["grande.pdf"])
        mundo.graph.borrador_403 = True
        r = await _enviar(mundo, plugin, pedido, ["grande.pdf"])
        assert r["success"] is False and r["codigo"] == "requiere_mail_readwrite", r
        assert "Mail.ReadWrite" in r["error"]
        assert not _peticiones(mundo, r"/send")
