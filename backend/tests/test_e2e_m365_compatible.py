from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_correo_central import _Mundo
from test_e2e_sharepoint_compat import DRIVE, _GraphOneDrive

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.user import User

pytestmark = [pytest.mark.integration, requires_database]

PLUGIN_SP = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
INTEGRACIONES_PRODUCCION = {"ms365-mail": {}, "ms365-calendar": {}, "ms365-teams": {}, "sharepoint": {}}


class _GraphDrive(_GraphOneDrive):
    def _por_ruta(self, ruta_archivo: str):
        return next(
            (
                i
                for i in type(self).items.values()
                if f"{i['parentReference']['path'].split('root:', 1)[1].strip('/')}/{i['name']}".strip("/")
                == ruta_archivo.strip("/")
            ),
            None,
        )

    def do_GET(self):
        ruta = self._ruta()
        if m := re.match(r"^/(?:me/drive|drives/[^/]+)/root:/(.+):/content$", ruta):
            self._registrar()
            item = self._por_ruta(m.group(1))
            if item is None:
                return self._json(404, {"error": {"code": "itemNotFound"}})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(item["datos"])))
            self.end_headers()
            self.wfile.write(item["datos"])
            return None
        if m := re.match(r"^/(?:me/drive|drives/[^/]+)/root:/(.+)$", ruta):
            self._registrar()
            item = self._por_ruta(m.group(1))
            if item is None:
                return self._json(404, {"error": {"code": "itemNotFound"}})
            return self._json(200, {k: v for k, v in item.items() if k != "datos"} | {"size": len(item["datos"])})
        if ruta == "/me/drives" or re.match(r"^/drives/[^/]+/root/search", ruta):
            self._registrar()
            if ruta == "/me/drives":
                return self._json(200, {"value": [{"id": DRIVE, "name": "OneDrive"}]})
            return self._json(200, {"value": []})
        return super().do_GET()

    def do_POST(self):
        if self._ruta() == "/search/query":
            cuerpo = json.loads(self._leer() or b"{}")
            self._registrar({"cuerpo": cuerpo})
            recurso = {
                "id": "item-sitio-1",
                "name": "Plan de Continuidad 2026.docx",
                "webUrl": "https://contoso.sharepoint.example/sites/Operaciones/Plan.docx",
                "size": 1234,
                "lastModifiedDateTime": "2026-10-01T10:00:00Z",
                "parentReference": {"driveId": "drive-sitio-operaciones", "siteId": "sitio-op"},
                "createdBy": {"user": {"email": "dato-que-no-debe-salir@contoso.example"}},
            }
            return self._json(200, {"value": [{"hitsContainers": [{"hits": [{"hitId": "h1", "resource": recurso}]}]}]})
        return super().do_POST()


class _MundoCompatible(_Mundo):
    def env_backend(self) -> dict:
        env = super().env_backend()
        env.update(
            {
                "APPROVAL_CENTRAL_ENABLED": "false",
                "APPROVAL_CENTRAL_ENFORCED": "false",
                "M365_AGENT_TOKEN_ENABLED": "true",
            }
        )
        return env

    async def sembrar(self) -> None:
        async with self.maker() as s:
            self.admin_id = (await s.execute(text("SELECT id FROM users WHERE username = 'admin'"))).scalar_one()
            nodo = Node(name="nodo-compatible", hostname="localhost")
            s.add(nodo)
            await s.flush()
            agente = Agent(
                node_id=nodo.id,
                name="agente-compatible",
                slug="agente-compatible",
                status="running",
                workspace_path=str(self.workspace),
                integration_configs=dict(INTEGRACIONES_PRODUCCION),
                enabled_toolsets=["hermeshq_ms365_mail", "hermeshq_sharepoint"],
            )
            s.add(agente)
            await s.flush()
            s.add(AgentAssignment(agent_id=agente.id, user_id=self.admin_id))
            await s.commit()
            self.agent_id = agente.id
            self.otra_id = self.admin_id


@pytest.fixture
async def compatible(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type(
        "GraphCompat",
        (_GraphDrive,),
        {"items": {}, "sesiones": {}, "permisos": {}, "peticiones": [], "modo": "normal", "host_subida": None},
    )
    m = _MundoCompatible(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        graph_cls=graph,
        tokens=True,
    )
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


def _cargar_sp():
    spec = importlib.util.spec_from_file_location(f"sp_compat_{uuid.uuid4().hex[:6]}", PLUGIN_SP)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


async def _sp(m: _MundoCompatible, mensaje: str = "envía"):
    task_id, lease = await m.tarea()
    m.plugin_para(task_id, lease, mensaje=mensaje)
    m.monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")
    m.monkeypatch.setenv("HERMESHQ_ENTORNO", "aislado")
    return task_id, _cargar_sp()


async def _llamar(plugin, herramienta, **args):
    return json.loads(await asyncio.to_thread(getattr(plugin, herramienta), args))


def _sembrar_archivo(m, carpeta: str, nombre: str, datos: bytes) -> None:
    iid = "item-" + hashlib.sha256(f"{carpeta}/{nombre}".encode()).hexdigest()[:10]
    m.graph.items[iid] = {
        "id": iid,
        "name": nombre,
        "datos": datos,
        "parentReference": {"driveId": DRIVE, "path": f"/drive/root:/{carpeta}"},
    }


class TestSharePointEnConfiguracionCompatible:
    async def test_subir_con_espacios_y_tildes(self, compatible):
        (compatible.workspace / "Informe final.pdf").write_bytes(b"%PDF-1.4 informe")
        _, sp = await _sp(compatible, mensaje="sube el informe final a la carpeta Año 2026 de mi OneDrive")
        r = await _llamar(sp, "_upload_file_tool", file_path="Informe final.pdf", folder="Año 2026")
        assert r["success"] is True, r
        assert any(i["name"] == "Informe final.pdf" for i in compatible.graph.items.values())

    async def test_subir_dos_veces_el_mismo_archivo_no_falla_ni_duplica(self, compatible):
        (compatible.workspace / "acta.pdf").write_bytes(b"%PDF-1.4 acta")
        _, sp = await _sp(compatible, mensaje="guarda el acta en Entregas")
        primera = await _llamar(sp, "_upload_file_tool", file_path="acta.pdf", folder="Entregas")
        segunda = await _llamar(sp, "_upload_file_tool", file_path="acta.pdf", folder="Entregas")
        assert primera["success"] is True and segunda["success"] is True, (primera, segunda)
        assert segunda.get("ya_estaba_subido") is True and segunda["url"] == primera["url"], segunda
        assert len([i for i in compatible.graph.items.values() if i["name"] == "acta.pdf"]) == 1

    async def test_caso_real_carpeta_inventada_en_la_raiz_no_se_crea(self, compatible):
        (compatible.workspace / "informe.html").write_bytes(b"<html>informe</html>")
        _sembrar_archivo(compatible, "Escritorio/Ana/Proyectos", "otro.pdf", b"%PDF-1.4 x")
        pedido = "guárdalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Proyectos"
        _, sp = await _sp(compatible, mensaje=pedido)
        r = await _llamar(sp, "_upload_file_tool", file_path="informe.html", folder="Ana/Proyectos")
        assert r["success"] is False and "no existe en la raíz" in r["error"], r
        assert not any(i["name"] == "informe.html" for i in compatible.graph.items.values())
        ok = await _llamar(sp, "_upload_file_tool", file_path="informe.html", folder="Escritorio/Ana/Proyectos")
        assert ok["success"] is True, ok
        assert any(i["name"] == "informe.html" for i in compatible.graph.items.values())

    async def test_descargar_por_ruta_con_espacios_y_tildes(self, compatible):
        _sembrar_archivo(compatible, "Año 2026", "Plan anual.pdf", b"%PDF-1.4 plan")
        _, sp = await _sp(compatible)
        r = await _llamar(sp, "_download_file_tool", file_path="Año 2026/Plan anual.pdf")
        assert r["success"] is True, r
        assert Path(r["local_path"]).read_bytes() == b"%PDF-1.4 plan"

    async def test_descargar_por_ruta_simple_devuelve_los_bytes_reales(self, compatible):
        _sembrar_archivo(compatible, "Docs", "a.pdf", b"%PDF-1.4 contenido-real")
        _, sp = await _sp(compatible)
        r = await _llamar(sp, "_download_file_tool", file_path="Docs/a.pdf")
        assert r["success"] is True, r
        assert Path(r["local_path"]).read_bytes() == b"%PDF-1.4 contenido-real"

    async def test_recorrido_de_directorios_sigue_rechazado(self, compatible):
        _, sp = await _sp(compatible)
        for ruta in ("Docs/../Finanzas/x.pdf", "Docs%2f..%2fFinanzas/x.pdf", "Docs/%252e%252e/x.pdf"):
            r = await _llamar(sp, "_download_file_tool", file_path=ruta)
            assert r["success"] is False, (ruta, r)
        assert not [p for p in compatible.graph.peticiones if "Finanzas" in p["ruta"] or ".." in p["ruta"]]


def _subidas_y_enlaces(m):
    return [p for p in m.graph.peticiones if p["metodo"] in ("PUT", "POST")]


class TestCompartirEnConfiguracionCompatible:
    async def test_pide_confirmacion_sin_subir_nada(self, compatible):
        (compatible.workspace / "informe.pdf").write_bytes(b"%PDF-1.4 compartir")
        _, sp = await _sp(compatible)
        r = await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        assert r["requiere_aprobacion"] is True and r["vista_previa"]["archivo"] == "informe.pdf", r
        assert "organización" in r["vista_previa"]["quien_puede_abrirlo"]
        assert not _subidas_y_enlaces(compatible), "no se sube nada antes de que el usuario confirme"

    async def test_confirmado_en_un_mensaje_posterior_crea_un_solo_enlace(self, compatible):
        (compatible.workspace / "informe.pdf").write_bytes(b"%PDF-1.4 compartir")
        _, sp = await _sp(compatible)
        await _llamar(sp, "_share_file_tool", file_path="informe.pdf")
        mismo_turno = await _llamar(sp, "_share_file_tool", file_path="informe.pdf", aprobado_por_el_usuario=True)
        assert mismo_turno["success"] is False and "no puedes confirmar" in mismo_turno["error"].lower(), mismo_turno
        assert not _subidas_y_enlaces(compatible)
        _, sp_cancelado = await _sp(compatible)
        carga = json.loads(os.environ["HERMESHQ_TASK_PAYLOAD"])
        compatible.monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", json.dumps({**carga, "mensaje": "olvídalo"}))
        cancelado = await _llamar(
            sp_cancelado, "_share_file_tool", file_path="informe.pdf", aprobado_por_el_usuario=True
        )
        assert cancelado["success"] is False and not _subidas_y_enlaces(compatible), cancelado
        task_id, sp2 = await _sp(compatible)
        carga = json.loads(os.environ["HERMESHQ_TASK_PAYLOAD"])
        compatible.monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", json.dumps({**carga, "mensaje": "sí, compártelo"}))
        r = await _llamar(sp2, "_share_file_tool", file_path="informe.pdf", aprobado_por_el_usuario=True)
        assert r["success"] is True and r["url"].endswith("/organization"), r
        enlaces = [p for p in compatible.graph.peticiones if p["ruta"].endswith("/createLink")]
        assert len(enlaces) == 1 and enlaces[0]["cuerpo"] == {"type": "view", "scope": "organization"}
        acciones = await compatible.acciones(task_id)
        assert any(a["kind"] == "graph:compartir" and a["state"] == "confirmada" for a in acciones), acciones

    async def test_una_tarea_programada_no_comparte(self, compatible):
        (compatible.workspace / "informe.pdf").write_bytes(b"%PDF-1.4 compartir")
        task_id, lease = await compatible.tarea()
        compatible.plugin_para(task_id, lease)
        compatible.monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")
        compatible.monkeypatch.setenv(
            "HERMESHQ_TASK_PAYLOAD",
            json.dumps({"task_id": task_id, "metadata": {"scheduled": True, "thread_user_id": compatible.admin_id}}),
        )
        r = await _llamar(_cargar_sp(), "_share_file_tool", file_path="informe.pdf", aprobado_por_el_usuario=True)
        assert r["success"] is False and "programada" in r["error"], r
        assert not _subidas_y_enlaces(compatible)

    async def test_el_broker_rechaza_otro_cuerpo_de_enlace(self, compatible):
        _, sp = await _sp(compatible)
        r = await asyncio.to_thread(
            sp._broker_request, "POST", "/me/drive/items/item-abcdef/createLink", {"type": "edit", "scope": "anonymous"}
        )
        assert "compartir_cuerpo_no_permitido" in str(r), r
        assert not [p for p in compatible.graph.peticiones if p["ruta"].endswith("/createLink")]

    async def test_con_aprobacion_central_el_broker_sigue_exigiendo_la_aprobacion_del_dueno(self, compatible):
        async with compatible.maker() as s:
            await s.execute(
                text("UPDATE agents SET approval_central_mode = 'activada' WHERE id = :a"), {"a": compatible.agent_id}
            )
            await s.commit()
        _, sp = await _sp(compatible)
        r = await asyncio.to_thread(
            sp._broker_request,
            "POST",
            "/me/drive/items/item-abcdef/createLink",
            {"type": "view", "scope": "organization"},
        )
        assert "accion_externa_requiere_aprobacion" in str(r), r
        assert not [p for p in compatible.graph.peticiones if p["ruta"].endswith("/createLink")]


async def _tarea_con_metadatos(m: _MundoCompatible, metadata: dict):
    from hermeshq.models.task import Task
    from hermeshq.services.execution_lease import ExecutionLeaseStore

    async with m.maker() as s:
        t = Task(
            agent_id=m.agent_id, prompt="comparte", status="running", created_by_user_id=None, metadata_json=metadata
        )
        s.add(t)
        await s.commit()
        task_id = t.id
    lease = await ExecutionLeaseStore(m.maker, owner="runner-e2e").begin(task_id, str(uuid.uuid4()), 1, 600)
    m.plugin_para(task_id, lease)
    m.monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")
    m.monkeypatch.setenv("HERMESHQ_ENTORNO", "aislado")
    m.monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", json.dumps({"task_id": task_id, "metadata": metadata}))
    return task_id, _cargar_sp()


def _enlace(sp, huella=None, scope="organization", item="item-abcdef"):
    return sp._broker_request(
        "POST", f"/me/drive/items/{item}/createLink", {"type": "view", "scope": scope}, confirmacion_compartir=huella
    )


def _enlaces(m):
    return [p for p in m.graph.peticiones if p["ruta"].endswith("/createLink")]


HUELLA = hashlib.sha256(b"informe").hexdigest()


class TestCompartirExigeConfirmacionEnElServidor:
    async def test_sin_vista_previa_registrada_el_broker_no_crea_enlaces(self, compatible):
        _, sp = await _sp(compatible)
        for scope in ("organization", "anonymous"):
            for huella in (None, HUELLA):
                r = await asyncio.to_thread(_enlace, sp, huella, scope)
                assert "compartir_requiere_confirmacion" in str(r), (scope, huella, r)
        assert not _enlaces(compatible)

    async def test_la_vista_previa_de_la_misma_tarea_no_confirma(self, compatible):
        _, sp = await _sp(compatible)
        assert await asyncio.to_thread(sp._registrar_vista_previa_en_servidor, HUELLA, "anonymous") is None
        r = await asyncio.to_thread(_enlace, sp, HUELLA, "anonymous")
        assert "compartir_requiere_confirmacion" in str(r), r
        assert not _enlaces(compatible)

    async def test_confirmada_en_una_tarea_posterior_crea_un_solo_enlace_y_no_se_reutiliza(self, compatible):
        _, sp = await _sp(compatible)
        assert await asyncio.to_thread(sp._registrar_vista_previa_en_servidor, HUELLA, "anonymous") is None
        _, sp2 = await _sp(compatible)
        r = await asyncio.to_thread(_enlace, sp2, HUELLA, "anonymous")
        assert 200 <= r.get("status", 0) < 300, r
        _, sp3 = await _sp(compatible)
        otra = await asyncio.to_thread(_enlace, sp3, HUELLA, "anonymous", "item-otroarchivo")
        assert "compartir_requiere_confirmacion" in str(otra), otra
        assert len(_enlaces(compatible)) == 1

    async def test_la_confirmacion_queda_ligada_al_alcance(self, compatible):
        _, sp = await _sp(compatible)
        assert await asyncio.to_thread(sp._registrar_vista_previa_en_servidor, HUELLA, "organization") is None
        _, sp2 = await _sp(compatible)
        r = await asyncio.to_thread(_enlace, sp2, HUELLA, "anonymous")
        assert "compartir_requiere_confirmacion" in str(r), r
        assert not _enlaces(compatible)

    async def test_la_confirmacion_queda_ligada_al_usuario(self, compatible):
        _, sp = await _sp(compatible)
        assert await asyncio.to_thread(sp._registrar_vista_previa_en_servidor, HUELLA, "organization") is None
        async with compatible.maker() as s:
            persona = User(username="otra-persona", display_name="Otra", password_hash="x", role="user")
            s.add(persona)
            await s.flush()
            otro = persona.id
            s.add(AgentAssignment(agent_id=compatible.agent_id, user_id=otro))
            await s.commit()
        task_id, lease = await compatible.tarea(usuario_id=otro)
        compatible.plugin_para(task_id, lease, usuario_id=otro)
        compatible.monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")
        r = await asyncio.to_thread(_enlace, _cargar_sp(), HUELLA, "organization")
        assert "compartir_requiere_confirmacion" in str(r), r
        assert not _enlaces(compatible)

    async def test_un_trabajo_en_segundo_plano_no_comparte_aunque_haya_vista_previa(self, compatible):
        _, sp = await _sp(compatible)
        assert await asyncio.to_thread(sp._registrar_vista_previa_en_servidor, HUELLA, "organization") is None
        for metadata in (
            {"background_job": True, "source": "background_job", "thread_user_id": compatible.admin_id},
            {"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": compatible.admin_id},
        ):
            _, spb = await _tarea_con_metadatos(compatible, metadata)
            r = await asyncio.to_thread(_enlace, spb, HUELLA, "organization")
            assert "compartir_en_tarea_automatica" in str(r), (metadata, r)
            assert await asyncio.to_thread(spb._registrar_vista_previa_en_servidor, HUELLA, "organization")
        assert not _enlaces(compatible)

    async def test_el_flujo_del_plugin_con_alcance_anonimo_sigue_funcionando(self, compatible):
        (compatible.workspace / "informe.pdf").write_bytes(b"%PDF-1.4 compartir")
        _, sp = await _sp(compatible)
        previa = await _llamar(sp, "_share_file_tool", file_path="informe.pdf", scope="anonymous")
        assert previa["requiere_aprobacion"] is True, previa
        assert "cualquiera" in previa["vista_previa"]["quien_puede_abrirlo"]
        assert not _subidas_y_enlaces(compatible)
        _, sp2 = await _sp(compatible, mensaje="sí, compártelo")
        r = await _llamar(
            sp2, "_share_file_tool", file_path="informe.pdf", scope="anonymous", aprobado_por_el_usuario=True
        )
        assert r["success"] is True and r["scope"] == "anonymous", r
        enlaces = _enlaces(compatible)
        assert len(enlaces) == 1 and enlaces[0]["cuerpo"] == {"type": "view", "scope": "anonymous"}

    async def test_el_agente_no_puede_saltarse_la_puerta_del_plugin(self, compatible):
        (compatible.workspace / "informe.pdf").write_bytes(b"%PDF-1.4 compartir")
        _, sp = await _sp(compatible)
        sp._puerta_de_compartir = lambda *_a, **_k: None
        r = await _llamar(sp, "_share_file_tool", file_path="informe.pdf", scope="anonymous")
        assert r["success"] is False and "compartir_requiere_confirmacion" in r["error"], r
        assert not _enlaces(compatible)


class TestBusquedaEnConfiguracionCompatible:
    async def test_encuentra_archivos_de_sitios_de_sharepoint_como_antes(self, compatible):
        _, sp = await _sp(compatible)
        r = await _llamar(sp, "_search_tool", query="Plan de Continuidad")
        assert r["success"] is True and r["count"] == 1, r
        assert r["hits"][0]["drive_id"] == "drive-sitio-operaciones" and r["hits"][0]["item_id"] == "item-sitio-1"
        assert "dato-que-no-debe-salir" not in json.dumps(r)

    async def test_el_broker_solo_admite_una_busqueda_de_archivos_acotada(self, compatible):
        _, sp = await _sp(compatible)
        for cuerpo in (
            {"requests": [{"entityTypes": ["message"], "query": {"queryString": "x"}}]},
            {"requests": [{"entityTypes": ["driveItem"], "query": {"queryString": "x"}}] * 2},
            {"requests": [{"entityTypes": ["driveItem"], "query": {"queryString": "x"}, "size": 500}]},
        ):
            r = await asyncio.to_thread(sp._broker_request, "POST", "/search/query", cuerpo)
            assert "busqueda_no_permitida" in str(r), (cuerpo, r)
        assert not [p for p in compatible.graph.peticiones if p["ruta"] == "/search/query"]

    async def test_con_politica_explicita_sigue_buscando_solo_en_sus_unidades(self, compatible):
        async with compatible.maker() as s:
            await s.execute(
                text("UPDATE agents SET integration_configs = :c WHERE id = :a"),
                {
                    "a": compatible.agent_id,
                    "c": json.dumps(
                        {
                            "sharepoint": {},
                            "m365_graph": {
                                "unidades": [DRIVE],
                                "operaciones": ["search"],
                                "carpetas_lectura": ["Docs"],
                            },
                        }
                    ),
                },
            )
            await s.commit()
        _, sp = await _sp(compatible)
        r = await _llamar(sp, "_search_tool", query="Plan de Continuidad")
        assert r["success"] is True and r["count"] == 0, r
        assert not [p for p in compatible.graph.peticiones if p["ruta"] == "/search/query"]


from test_e2e_correo_migrado import _GraphCorreo  # noqa: E402

CUERPO_LARGO = "<p>" + ("Detalle del contrato. " * 3000) + "</p>"


class _GraphBuzon(_GraphCorreo):
    busquedas: list = []

    def do_GET(self):
        ruta = self.path.split("?", 1)[0].replace("/v1", "", 1)
        consulta = urllib.parse.parse_qs(self.path.partition("?")[2])
        if ruta == "/me/mailFolders/SUB-ID/messages" and "$search" not in consulta:
            self._registrar()
            return self._json(200, {"value": [{"id": "msg-proyectos-0001", "subject": "Avance de Proyectos"}]})
        if ruta in ("/me/messages", "/me/mailFolders/SUB-ID/messages") and "$search" in consulta:
            self._registrar()
            type(self).busquedas.append({"ruta": ruta, "search": consulta["$search"][0], "top": consulta.get("$top")})
            top = int((consulta.get("$top") or ["10"])[0])
            filas = [
                {
                    "id": f"msg-{n:08d}",
                    "subject": f"propuesta {n}",
                    "parentFolderId": "INBOX-ID",
                    "receivedDateTime": f"2026-10-{1 + n % 28:02d}T10:00:00Z",
                }
                for n in range(45)
            ]
            filas[0]["parentFolderId"] = "SENT-ID"
            return self._json(200, {"value": filas[:top]})
        if ruta == "/me/messages/largo-00000001":
            self._registrar()
            return self._json(
                200,
                {
                    "id": "largo-00000001",
                    "subject": "Contrato",
                    "parentFolderId": "INBOX-ID",
                    "body": {"contentType": "html", "content": CUERPO_LARGO},
                    "webLink": "https://outlook.office.example/?id=largo",
                    "from": {"emailAddress": {"address": "a@hq.example"}},
                    "toRecipients": [],
                },
            )
        return super().do_GET()


@pytest.fixture
async def buzon(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    graph = type("GraphBuzon", (_GraphBuzon,), {"borradores": {}, "marcados": [], "busquedas": []})
    m = _MundoCompatible(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        graph_cls=graph,
        tokens=True,
    )
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


async def _correo(m):
    task_id, lease = await m.tarea()
    plugin = m.plugin_para(task_id, lease)
    m.monkeypatch.setenv("HERMESHQ_APROBACION_CENTRAL", "0")
    return plugin


class TestCorreoEnConfiguracionCompatible:
    async def test_listar_una_carpeta_propia_por_nombre_y_por_ruta(self, buzon):
        correo = await _correo(buzon)
        for carpeta in ("Proyectos", "Bandeja de entrada/Proyectos"):
            r = await _llamar(correo, "_list_emails_tool", folder=carpeta)
            assert r["success"] is True, (carpeta, r)
            assert [m["id"] for m in r["messages"]] == ["msg-proyectos-0001"]

    async def test_carpeta_inexistente_dice_cuales_hay(self, buzon):
        correo = await _correo(buzon)
        r = await _llamar(correo, "_list_emails_tool", folder="Finanzas")
        assert r["success"] is False and "Proyectos" in r["error"], r

    async def test_buscar_sin_carpeta_busca_en_todo_el_buzon(self, buzon):
        correo = await _correo(buzon)
        r = await _llamar(correo, "_search_emails_tool", query="propuesta", count=40)
        assert r["success"] is True and r["count"] == 40, r
        assert buzon.graph.busquedas[-1]["ruta"] == "/me/messages"
        assert "msg-00000000" in [m["id"] for m in r["messages"]], "incluye el que está en Enviados"

    async def test_buscar_en_una_carpeta_propia(self, buzon):
        correo = await _correo(buzon)
        r = await _llamar(correo, "_search_emails_tool", query="propuesta", folder="Proyectos")
        assert r["success"] is True, r
        assert buzon.graph.busquedas[-1]["ruta"] == "/me/mailFolders/SUB-ID/messages"

    async def test_textos_de_busqueda_comunes_se_aceptan(self, buzon):
        correo = await _correo(buzon)
        for texto in (
            "RE: propuesta",
            "O'Brien",
            "N°123",
            "informe Q3/2026",
            "5",
            "acta reunión directorio comité riesgos operacionales noviembre",
        ):
            r = await _llamar(correo, "_search_emails_tool", query=texto)
            assert r["success"] is True, (texto, r)

    async def test_la_gramatica_sigue_bloqueando_operadores_e_inyeccion(self, buzon):
        correo = await _correo(buzon)
        for texto in ("propuesta OR participants:x", 'x" OR "y', "a\\b", "propuesta NOT contrato"):
            r = await _llamar(correo, "_search_emails_tool", query=texto)
            assert r["success"] is False, (texto, r)

    async def test_leer_un_correo_largo_respeta_max_chars_y_trae_el_enlace(self, buzon):
        correo = await _correo(buzon)
        r = await _llamar(correo, "_get_email_tool", message_id="largo-00000001", format="html", max_chars=60000)
        assert r["success"] is True, r
        cuerpo = json.dumps(r, ensure_ascii=False)
        assert cuerpo.count("Detalle del contrato.") >= 2500, "el cuerpo no debe cortarse a 20.000 caracteres"
        assert "https://outlook.office.example/?id=largo" in cuerpo

    async def test_con_politica_solo_bandeja_buscar_sin_carpeta_busca_en_la_bandeja(self, buzon):
        async with buzon.maker() as s:
            await s.execute(
                text("UPDATE agents SET integration_configs = :c WHERE id = :a"),
                {
                    "a": buzon.agent_id,
                    "c": json.dumps(
                        {
                            "ms365-mail": {},
                            "m365_graph": {"correo": {"operaciones": ["mail_search"], "carpetas": ["inbox"]}},
                        }
                    ),
                },
            )
            await s.commit()
        correo = await _correo(buzon)
        r = await _llamar(correo, "_search_emails_tool", query="presupuesto")
        assert r["success"] is True, r
        rutas = [
            p["ruta"].split("?", 1)[0].replace("/v1", "", 1) for p in buzon.graph.peticiones if "messages" in p["ruta"]
        ]
        assert rutas and rutas[-1] == "/me/mailFolders/inbox/messages", rutas
