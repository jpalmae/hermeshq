from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_calendario_teams import _aprobar
from test_e2e_correo_central import _Mundo

pytestmark = [pytest.mark.integration, requires_database]

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
DRIVE = "drive-yo"
POLITICA = {
    "unidades": [DRIVE],
    "carpetas_escritura": ["UltraBot-archivos", "Entregas"],
    "operaciones": ["upload"],
    "correo": {"operaciones": ["mail_list"], "carpetas": ["inbox"]},
}
MB = 1_000_000


class _GraphOneDrive(BaseHTTPRequestHandler):
    modo = "normal"
    items: dict = {}
    sesiones: dict = {}
    permisos: dict = {}
    peticiones: list = []
    host_subida: str | None = None

    def log_message(self, *a):
        pass

    def _json(self, estado, datos):
        cuerpo = json.dumps(datos).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _ruta(self):
        return urllib.parse.unquote(self.path.split("?", 1)[0].replace("/v1", "", 1))

    def _registrar(self, extra=None):
        type(self).peticiones.append(
            {
                "metodo": self.command,
                "ruta": self._ruta(),
                "query": self.path.partition("?")[2],
                "auth": self.headers.get("Authorization"),
                **(extra or {}),
            }
        )

    def _guardar(self, ruta_archivo: str, datos: bytes) -> dict:
        iid = "item-" + hashlib.sha256(ruta_archivo.encode()).hexdigest()[:10]
        carpeta, _, nombre = ruta_archivo.rpartition("/")
        type(self).items[iid] = {
            "id": iid,
            "name": nombre,
            "datos": datos,
            "parentReference": {"driveId": DRIVE, "path": f"/drive/root:/{carpeta}"},
        }
        return {k: v for k, v in type(self).items[iid].items() if k != "datos"}

    def do_GET(self):
        ruta = self._ruta()
        self._registrar()
        if ruta == "/me/drive":
            return self._json(200, {"id": DRIVE})
        if m := re.match(r"^/(?:me/drive|drives/[^/]+)/items/([^/]+)/permissions$", ruta):
            return self._json(200, {"value": type(self).permisos.get(m.group(1), [])})
        if m := re.match(r"^/(?:me/drive|drives/[^/]+)/items/([^/]+)$", ruta):
            item = type(self).items.get(m.group(1))
            return self._json(200, {k: v for k, v in item.items() if k != "datos"}) if item else self._json(404, {})
        return self._json(404, {})

    def do_PUT(self):
        ruta = self._ruta()
        datos = self._leer()
        self._registrar({"bytes": len(datos), "rango": self.headers.get("Content-Range")})
        cls = type(self)
        if m := re.match(r"^/drives/[^/]+/root:/(.+):/content$", ruta):
            return self._json(201, self._guardar(m.group(1), datos))
        if m := re.match(r"^/subida/([0-9a-f]+)$", ruta):
            sesion = cls.sesiones[m.group(1)]
            sesion["datos"].extend(datos)
            total = int(self.headers["Content-Range"].rsplit("/", 1)[1])
            if len(sesion["datos"]) == total:
                return self._json(201, self._guardar(sesion["ruta"], bytes(sesion["datos"])))
            return self._json(202, {"nextExpectedRanges": [f"{len(sesion['datos'])}-"]})
        return self._json(404, {})

    def do_POST(self):
        ruta = self._ruta()
        cuerpo = json.loads(self._leer() or b"{}")
        self._registrar({"cuerpo": cuerpo})
        cls = type(self)
        if m := re.match(r"^/drives/[^/]+/root:/(.+):/createUploadSession$", ruta):
            sid = uuid.uuid4().hex[:8]
            cls.sesiones[sid] = {"ruta": m.group(1), "datos": bytearray()}
            base = cls.host_subida or f"http://127.0.0.1:{self.server.server_address[1]}"
            return self._json(200, {"uploadUrl": f"{base}/subida/{sid}"})
        if m := re.match(r"^/me/drive/items/([^/]+)/createLink$", ruta):
            enlace = {
                "type": cuerpo["type"],
                "scope": cuerpo["scope"],
                "webUrl": f"https://contoso-my.sharepoint.example/:b:/{m.group(1)}/{cuerpo['scope']}",
            }
            existentes = cls.permisos.setdefault(m.group(1), [])
            if not any(p["link"] == enlace for p in existentes):
                existentes.append({"id": f"perm-{len(existentes) + 1}", "link": enlace})
            if cls.modo == "timeout_tras_aceptar":
                self.connection.close()
                return None
            return self._json(201, {"link": enlace})
        return self._json(404, {})


def _cargar():
    spec = importlib.util.spec_from_file_location(f"sp_e2e_{uuid.uuid4().hex[:6]}", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphOD",
        (_GraphOneDrive,),
        {"items": {}, "sesiones": {}, "permisos": {}, "peticiones": [], "modo": "normal", "host_subida": None},
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


async def _sp(m: _Mundo):
    task_id, lease = await m.tarea()
    m.plugin_para(task_id, lease)
    m.monkeypatch.setenv("HERMESHQ_ENTORNO", "aislado")
    return task_id, _cargar()


async def _llamar(plugin, herramienta, **args):
    return json.loads(await asyncio.to_thread(getattr(plugin, herramienta), args))


def _archivo(m: _Mundo, nombre: str, tam: int) -> bytes:
    datos = (hashlib.sha256(nombre.encode()).digest() * (tam // 32 + 1))[:tam]
    (m.workspace / nombre).write_bytes(datos)
    return datos


def _enlaces(m) -> list:
    return [p for p in m.graph.peticiones if p["ruta"].endswith("/createLink")]


class TestSubidasGrandes:
    async def test_subir_10_mb_usa_sesion_por_trozos_sin_token(self, mundo):
        datos = _archivo(mundo, "informe.pdf", 10 * MB)
        _, sp = await _sp(mundo)
        r = await _llamar(sp, "_upload_file_tool", file_path="informe.pdf", folder="Entregas")
        assert r["success"] is True, r
        (item,) = (i for i in mundo.graph.items.values() if i["name"] == "informe.pdf")
        assert item["datos"] == datos
        trozos = [p for p in mundo.graph.peticiones if p["ruta"].startswith("/subida/")]
        assert len(trozos) >= 3 and all(p["auth"] is None for p in trozos)

    async def test_url_de_carga_ajena_no_recibe_bytes(self, mundo):
        _archivo(mundo, "informe.pdf", 5 * MB)
        mundo.graph.host_subida = "https://atacante.invalid"
        _, sp = await _sp(mundo)
        r = await _llamar(sp, "_upload_file_tool", file_path="informe.pdf", folder="Entregas")
        assert r["success"] is False and "no es de Microsoft" in r["error"]
        assert not [p for p in mundo.graph.peticiones if p["ruta"].startswith("/subida/")]

    async def test_subir_fuera_de_la_carpeta_permitida_sigue_denegado(self, mundo):
        _archivo(mundo, "informe.pdf", 5 * MB)
        _, sp = await _sp(mundo)
        r = await _llamar(sp, "_upload_file_tool", file_path="informe.pdf", folder="Privado")
        assert r["success"] is False
        assert not mundo.graph.items


class TestCompartirConAprobacion:
    async def test_compartir_pide_aprobacion_con_archivo_y_alcance_y_luego_crea_un_enlace(self, mundo):
        datos = _archivo(mundo, "informe.pdf", 200_000)
        task_id, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        assert pedido["requiere_aprobacion"] is True, pedido
        assert not _enlaces(mundo)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        nombre = f"{hashlib.sha256(datos).hexdigest()[:8]}_informe.pdf"
        assert fila["tipo_accion"] == "graph.onedrive"
        assert fila["contenido_json"]["archivo"] == {"nombre": nombre, "ruta": "UltraBot-archivos"}
        assert f"UltraBot-archivos/{nombre}" in fila["vista_previa"]
        assert "solo personas de la organización" in fila["vista_previa"]
        await _aprobar(mundo, pedido)
        _, sp2 = await _sp(mundo)
        r = await _llamar(sp2, "_share_file_tool", file_path="informe.pdf", aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True and r["url"].endswith("/organization"), r
        assert len(_enlaces(mundo)) == 1 and _enlaces(mundo)[0]["cuerpo"] == {"type": "view", "scope": "organization"}
        assert len([i for i in mundo.graph.items.values() if i["name"] == nombre]) == 1
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "ejecutada"

    async def test_archivo_grande_compartido_por_sesion(self, mundo):
        datos = _archivo(mundo, "deck.pptx", 8 * MB)
        _, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="deck.pptx")
        assert pedido["requiere_aprobacion"] is True, pedido
        (item,) = mundo.graph.items.values()
        assert item["datos"] == datos

    async def test_cambiar_el_alcance_tras_aprobar_pide_aprobacion_nueva(self, mundo):
        _archivo(mundo, "informe.pdf", 100_000)
        _, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        await _aprobar(mundo, pedido)
        r = await _llamar(
            sp, "_share_file_tool", file_path="informe.pdf", scope="anonymous", aprobacion_id=pedido["aprobacion_id"]
        )
        assert r["requiere_aprobacion"] is True and r["aprobacion_id"] != pedido["aprobacion_id"]
        assert "cualquiera que tenga el enlace" in r["borrador"]
        assert [e["cuerpo"]["scope"] for e in _enlaces(mundo)] == ["organization"]

    async def test_timeout_al_crear_enlace_se_concilia_sin_repetir(self, mundo):
        _archivo(mundo, "informe.pdf", 100_000)
        task_id, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        mundo.graph.modo = "timeout_tras_aceptar"
        await _aprobar(mundo, pedido)
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["envio_estado"] == "desconocida"
        mundo.graph.modo = "normal"
        r = await _llamar(sp, "_share_file_tool", file_path="informe.pdf", aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True, r
        assert len(_enlaces(mundo)) == 1
        assert "conciliada_confirmada" in await mundo.eventos_aprobacion(pedido["aprobacion_id"])

    async def test_politica_puede_bloquear_compartir(self, mundo):
        async with mundo.maker() as s:
            from sqlalchemy import text

            await s.execute(
                text("UPDATE agents SET approval_policy = :p WHERE id = :i"),
                {"p": json.dumps({"onedrive.compartir": "bloqueada"}), "i": mundo.agent_id},
            )
            await s.commit()
        _archivo(mundo, "informe.pdf", 100_000)
        _, sp = await _sp(mundo)
        r = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        assert r["success"] is False and r["codigo"] == "operacion_bloqueada", r
        assert not _enlaces(mundo)

    async def test_crear_enlace_directo_por_el_proxy_sigue_prohibido(self, mundo):
        _, sp = await _sp(mundo)
        r = await asyncio.to_thread(
            sp._graph,
            "POST",
            "/me/drive/items/item-1234/createLink",
            sp.BROKER_TOKEN,
            {"type": "view", "scope": "anonymous"},
        )
        assert "error" in r
        assert not _enlaces(mundo)

    async def test_archivo_cambiado_tras_aprobar_se_sube_de_nuevo_y_pide_aprobacion(self, mundo):
        _archivo(mundo, "informe.pdf", 100_000)
        _, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        await _aprobar(mundo, pedido)
        _archivo(mundo, "informe.pdf", 100_001)
        r = await _llamar(sp, "_share_file_tool", file_path="informe.pdf", aprobacion_id=pedido["aprobacion_id"])
        assert r["requiere_aprobacion"] is True and r["aprobacion_id"] != pedido["aprobacion_id"], r
        assert len(mundo.graph.items) == 2 and len(_enlaces(mundo)) == 1

    async def test_repetir_sin_aprobacion_id_tras_aprobar_comparte_una_vez(self, mundo):
        _archivo(mundo, "informe.pdf", 100_000)
        _, sp = await _sp(mundo)
        pedido = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        await _aprobar(mundo, pedido)
        _, sp2 = await _sp(mundo)
        r = await _llamar(sp2, "_share_file_tool", file_path="informe.pdf")
        assert r["success"] is True and r["url"].endswith("/organization"), r
        assert len(_enlaces(mundo)) == 1
        assert len(mundo.graph.items) == 1
