from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import _scratch_url, requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import ADMIN_PASSWORD, _Instance, _login

from hermeshq.config import get_settings
from hermeshq.core.security import create_agent_service_token, hash_password
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.execution_lease import ExecutionLeaseStore

pytestmark = [pytest.mark.integration, requires_database]

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
TOKEN_SERVIDOR = "token-graph-del-servidor"
FIRMA_HTML = "<div class='firma'>Ana Pérez · Finanzas</div>"
LOGO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Graph(BaseHTTPRequestHandler):
    modo = "202"
    enviados: list = []
    peticiones: list = []
    falla_lectura_enviados = False

    def log_message(self, *a):
        pass

    def _registrar(self, cuerpo=None):
        type(self).peticiones.append(
            {"metodo": self.command, "ruta": self.path, "auth": self.headers.get("Authorization"), "cuerpo": cuerpo}
        )

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(largo) or b"{}")
        self._registrar(cuerpo)
        if self.path.endswith("/me/sendMail"):
            modo = type(self).modo
            if modo in ("202", "timeout_tras_aceptar"):
                type(self).enviados.append(cuerpo["message"])
            if modo == "timeout_tras_aceptar":
                self.connection.close()
                return
            if modo == "falla_antes":
                self.connection.close()
                return
            estado = {"202": 202, "500": 500, "400": 400}[modo]
            self.send_response(estado)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        self._registrar()
        if type(self).falla_lectura_enviados and "/mailFolders/sentitems/messages" in self.path:
            self.connection.close()
            return
        if "/mailFolders/inbox/messages" in self.path:
            datos = json.dumps({"value": [{"id": "m1", "subject": "Hola", "bodyPreview": "texto"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
            return
        if "/mailFolders/sentitems/messages" in self.path:
            valor = [
                {"id": f"enviado-{i}", "internetMessageHeaders": m.get("internetMessageHeaders") or []}
                for i, m in enumerate(type(self).enviados)
            ]
            datos = json.dumps({"value": valor}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
            return
        self.send_response(404)
        self.end_headers()


def _cargar_plugin():
    spec = importlib.util.spec_from_file_location(f"plugin_correo_e2e_{uuid.uuid4().hex[:6]}", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Mundo:
    def __init__(self, maker, db_name, tmp_path, monkeypatch, graph_cls=None, politica=None, tokens=False):
        self.tokens = tokens
        self.politica = politica or {"correo": {"operaciones": ["mail_list"], "carpetas": ["inbox"]}}
        self.maker = maker
        self.db_name = db_name
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.workspace = tmp_path / "workspaces" / "agente-correo"
        (self.workspace / "work" / "firma").mkdir(parents=True)
        (self.workspace / "work" / "firma" / "logo.png").write_bytes(LOGO)
        self.escribir_firma(FIRMA_HTML)
        (self.workspace / "informe.txt").write_text("informe trimestral sintético\n", encoding="utf-8")
        self.graph = type("Graph", (graph_cls or _Graph,), {"modo": "202", "enviados": [], "peticiones": []})
        self.servidor_graph = ThreadingHTTPServer(("127.0.0.1", 0), self.graph)
        threading.Thread(target=self.servidor_graph.serve_forever, daemon=True).start()
        self.instancias: list[_Instance] = []
        self.backend: _Instance | None = None

    def escribir_firma(self, html: str) -> None:
        manifiesto = {"personal": {"imagenes": [{"content_id": "logo", "path": "work/firma/logo.png"}], "html": html}}
        (self.workspace / "work" / "firma" / "manifest.json").write_text(json.dumps(manifiesto), encoding="utf-8")

    def env_backend(self) -> dict:
        return {
            "APPROVAL_CENTRAL_ENABLED": "true",
            "APPROVAL_RECONCILE_WAIT_SECONDS": "0",
            "M365_AGENT_TOKEN_ENABLED": "true" if self.tokens else "false",
            "HERMESHQ_ENTORNO": "aislado",
            "HERMESHQ_GRAPH_BASE_SIMULADO": f"http://127.0.0.1:{self.servidor_graph.server_address[1]}/v1",
            "HERMESHQ_GRAPH_TOKEN_SIMULADO": TOKEN_SERVIDOR,
            "JWT_SECRET": get_settings().jwt_secret,
        }

    async def arrancar_backend(self, nombre: str = "backend") -> _Instance:
        inst = _Instance(
            nombre,
            _scratch_url(self.db_name),
            self.workspace,
            "http://127.0.0.1:9/v1",
            self.tmp,
            extra_env=self.env_backend(),
        )
        self.instancias.append(inst)
        await inst.wait_ready()
        self.backend = inst
        return inst

    async def sembrar(self) -> None:
        async with self.maker() as s:
            self.admin_id = (await s.execute(text("SELECT id FROM users WHERE username = 'admin'"))).scalar_one()
            otra = User(username="otra-usuaria", display_name="Otra", password_hash=hash_password(ADMIN_PASSWORD))
            nodo = Node(name="nodo-correo", hostname="localhost")
            s.add_all([otra, nodo])
            await s.flush()
            agente = Agent(
                node_id=nodo.id,
                name="agente-correo",
                slug="agente-correo",
                status="running",
                workspace_path=str(self.workspace),
                integration_configs={"m365_graph": self.politica},
            )
            s.add(agente)
            await s.flush()
            s.add_all(
                [
                    AgentAssignment(agent_id=agente.id, user_id=self.admin_id),
                    AgentAssignment(agent_id=agente.id, user_id=otra.id),
                ]
            )
            await s.commit()
            self.otra_id, self.agent_id = otra.id, agente.id

    async def tarea(self, usuario_id: str | None = None, conversacion: str = "conv-correo", mensaje: str = "envía"):
        usuario_id = usuario_id or self.admin_id
        async with self.maker() as s:
            t = Task(
                agent_id=self.agent_id,
                prompt=mensaje,
                status="running",
                created_by_user_id=usuario_id,
                metadata_json={"conversation": True, "thread_id": conversacion, "thread_user_id": usuario_id},
            )
            s.add(t)
            await s.commit()
            task_id = t.id
        lease = await ExecutionLeaseStore(self.maker, owner="runner-e2e").begin(task_id, str(uuid.uuid4()), 1, 600)
        return task_id, lease

    def plugin_para(self, task_id: str, lease, usuario_id: str | None = None, mensaje: str = "envía"):
        usuario_id = usuario_id or self.admin_id
        entorno = {
            "HERMESHQ_INTERNAL_API_URL": f"{self.backend.base}/api/internal",
            "HERMESHQ_AGENT_ID": self.agent_id,
            "HERMESHQ_AGENT_TOKEN": create_agent_service_token(self.agent_id),
            "HERMESHQ_ATTEMPT_ID": lease.attempt_id,
            "HERMESHQ_LEASE_GENERATION": str(lease.generation),
            "HERMESHQ_APROBACION_CENTRAL": "1",
            "HERMES_SESSION_CWD": str(self.workspace),
            "HERMESHQ_TASK_PAYLOAD": json.dumps(
                {
                    "task_id": task_id,
                    "prompt": mensaje,
                    "mensaje": mensaje,
                    "metadata": {"thread_user_id": usuario_id, "thread_id": "conv-correo"},
                }
            ),
        }
        for clave, valor in entorno.items():
            self.monkeypatch.setenv(clave, valor)
        plugin = _cargar_plugin()

        def _prohibido(*_a, **_k):
            raise AssertionError("el plugin pidió un token de Graph en modo central")

        plugin._get_m365_token = _prohibido
        plugin._graph_directo_prohibido = True
        return plugin

    def enviar(self, plugin, **args) -> dict:
        base = {
            "to": "destino@cliente.example",
            "subject": "Informe trimestral",
            "body": "Hola,\n\nAdjunto el informe.\n\nSaludos",
            "attachments": [{"path": "informe.txt"}],
        }
        base.update(args)
        return json.loads(plugin._send_email_tool(base))

    async def aprobacion(self, aprob_id: str):
        async with self.maker() as s:
            return (await s.execute(text("SELECT * FROM approvals WHERE id = :i"), {"i": aprob_id})).mappings().one()

    async def acciones(self, task_id: str | None = None):
        async with self.maker() as s:
            sql = "SELECT * FROM external_action_records" + (" WHERE task_id = :t" if task_id else "")
            return (await s.execute(text(sql + " ORDER BY created_at"), {"t": task_id})).mappings().all()

    async def eventos_aprobacion(self, aprob_id: str) -> list[str]:
        async with self.maker() as s:
            filas = await s.execute(
                text("SELECT evento FROM approval_events WHERE approval_id = :i ORDER BY creado_en"), {"i": aprob_id}
            )
            return [f[0] for f in filas.all()]

    async def decidir(self, aprob_id: str, aprobar: bool = True, usuario: str = "admin"):
        headers = await _login_como(self.backend.base, usuario)
        aprob = await self.aprobacion(aprob_id)
        async with httpx.AsyncClient() as c:
            return await c.post(
                f"{self.backend.base}/api/aprobaciones/{aprob_id}/{'aprobar' if aprobar else 'rechazar'}",
                headers=headers,
                json={"huella": aprob["huella"], "conversacion_id": aprob["conversacion_id"]},
            )

    def envios_graph(self) -> int:
        return sum(1 for p in self.graph.peticiones if p["ruta"].endswith("/me/sendMail"))

    def cerrar(self):
        for inst in self.instancias:
            inst.kill_group()
        self.servidor_graph.shutdown()


async def _login_como(base: str, usuario: str) -> dict:
    if usuario == "admin":
        return await _login(base)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/api/auth/login", json={"username": usuario, "password": ADMIN_PASSWORD})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    m = _Mundo(async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession), db_name, tmp_path, monkeypatch)
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


async def _pedido_y_aprobado(m: _Mundo, **args):
    task_id, lease = await m.tarea()
    plugin = m.plugin_para(task_id, lease)
    pedido = await asyncio.to_thread(m.enviar, plugin, **args)
    assert pedido["requiere_aprobacion"] is True, pedido
    r = await m.decidir(pedido["aprobacion_id"])
    assert r.status_code == 200, r.text
    return task_id, lease, plugin, pedido


class TestFlujoDeAprobacion:
    async def test_01_02_solicitud_crea_aprobacion_persistida_sin_enviar(self, mundo):
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        r = await asyncio.to_thread(mundo.enviar, plugin, cc="jefa@cliente.example")
        assert r["requiere_aprobacion"] is True and r["success"] is False
        assert mundo.envios_graph() == 0
        fila = await mundo.aprobacion(r["aprobacion_id"])
        canon = fila["contenido_json"]
        assert (fila["estado"], fila["agent_id"], fila["propietario_user_id"]) == (
            "pendiente",
            mundo.agent_id,
            mundo.admin_id,
        )
        assert (fila["task_id_origen"], fila["attempt_id_origen"]) == (task_id, lease.attempt_id)
        assert canon["para"] == ["destino@cliente.example"] and canon["cc"] == ["jefa@cliente.example"]
        assert canon["asunto"] == "Informe trimestral" and "Adjunto el informe." in canon["cuerpo_html"]
        assert FIRMA_HTML in canon["cuerpo_html"]
        nombres = {a["nombre"]: a for a in canon["adjuntos"]}
        import hashlib

        assert (
            nombres["informe.txt"]["sha256"]
            == hashlib.sha256((mundo.workspace / "informe.txt").read_bytes()).hexdigest()
        )
        assert fila["expira_en"] > fila["creado_en"]
        assert r["borrador"] == fila["vista_previa"] and "destino@cliente.example" in r["borrador"]

    async def test_03_rechazo_impide_el_envio(self, mundo):
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        assert (await mundo.decidir(pedido["aprobacion_id"], aprobar=False)).status_code == 200
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and r["codigo"] == "rechazada"
        assert mundo.envios_graph() == 0
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["decidido_por_user_id"] == mundo.admin_id

    async def test_04_13_aprobacion_valida_envia_lo_aprobado_y_202_no_es_entrega(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True, r
        assert r["estado_envio"] == "aceptada_proveedor" and r["entregado"] is False
        assert "todavía no" in r["message"].lower() and "entregado" not in r["message"].lower()
        assert mundo.envios_graph() == 1
        enviado = mundo.graph.enviados[0]
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert enviado["body"]["content"] == fila["contenido_json"]["cuerpo_html"]
        assert FIRMA_HTML in enviado["body"]["content"]
        adj = {a["name"]: base64.b64decode(a["contentBytes"]) for a in enviado["attachments"]}
        assert adj["informe.txt"] == (mundo.workspace / "informe.txt").read_bytes()
        assert any(a.get("isInline") and a.get("contentId") == "logo" for a in enviado["attachments"])
        assert {"name": "x-hermeshq-aprobacion", "value": pedido["aprobacion_id"]} in enviado["internetMessageHeaders"]
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutada", "aceptada_proveedor")
        assert (fila["task_id_ejecucion"], fila["attempt_id_ejecucion"], fila["lease_generation_ejecucion"]) == (
            task_id,
            lease.attempt_id,
            lease.generation,
        )
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["confirmada"]


class TestContenidoInmutable:
    @pytest.mark.parametrize(
        "cambio",
        [
            {"body": "Hola,\n\nAdjunto el informe CORREGIDO.\n\nSaludos"},
            {"to": "otra-persona@cliente.example"},
            "adjunto",
            "firma",
        ],
        ids=["05_cuerpo", "06_destinatarios", "07_adjuntos", "08_firma"],
    )
    async def test_cambio_despues_de_aprobar_exige_aprobacion_nueva(self, mundo, cambio):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        args = {"aprobacion_id": pedido["aprobacion_id"]}
        if cambio == "adjunto":
            (mundo.workspace / "informe.txt").write_text("informe ALTERADO\n", encoding="utf-8")
        elif cambio == "firma":
            mundo.escribir_firma("<div class='firma'>Firma cambiada</div>")
        else:
            args.update(cambio)
        r = await asyncio.to_thread(mundo.enviar, plugin, **args)
        assert r["success"] is False and r["requiere_aprobacion"] is True, r
        assert r["aprobacion_id"] != pedido["aprobacion_id"]
        assert "cambió" in r["error"]
        assert mundo.envios_graph() == 0
        vieja = await mundo.aprobacion(pedido["aprobacion_id"])
        assert vieja["estado"] == "expirada" and vieja["reemplazada_por"] == r["aprobacion_id"]

    async def test_09_aprobacion_vencida_no_envia(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        async with mundo.maker() as s:
            await s.execute(
                text("UPDATE approvals SET expira_en = now() - interval '1 minute' WHERE id = :i"),
                {"i": pedido["aprobacion_id"]},
            )
            await s.commit()
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and r["requiere_aprobacion"] is True and "expiró" in r["error"]
        assert mundo.envios_graph() == 0


class TestFencing:
    async def test_10_intento_obsoleto_no_envia(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET state = 'abandoned', "
                    "lease_generation = lease_generation + 1 WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and r["codigo"] == "intento_no_vigente"
        assert mundo.envios_graph() == 0
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "aprobada"

    async def test_11_lease_expirado_no_envia(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 second' "
                    "WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["codigo"] == "intento_no_vigente" and mundo.envios_graph() == 0

    async def test_12_dos_envios_concurrentes_identicos_salen_una_vez(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        resultados = await asyncio.gather(
            *(asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"]) for _ in range(4))
        )
        assert sum(1 for r in resultados if r["success"]) == 1, resultados
        assert mundo.envios_graph() == 1
        assert all(r.get("ya_enviado") or r["success"] or r.get("estado_envio") == "desconocida" for r in resultados)


class TestResultadosInciertos:
    async def test_14_15_17_18_timeout_tras_aceptar_reinicio_reintento_y_conciliacion(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "timeout_tras_aceptar"
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and r["estado_envio"] == "desconocida" and r["ejecutado"] is None
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutando", "desconocida")
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["iniciada"]

        mundo.backend.kill_group()
        await mundo.arrancar_backend("backend-reiniciado")
        mundo.graph.modo = "202"
        otra_tarea, otro_lease = await mundo.tarea(mensaje="sí, reenvíalo")
        plugin2 = mundo.plugin_para(otra_tarea, otro_lease, mensaje="sí, reenvíalo")
        reintento = await asyncio.to_thread(mundo.enviar, plugin2, aprobacion_id=pedido["aprobacion_id"])
        assert reintento["success"] is False and (
            reintento.get("ya_enviado") or reintento["estado_envio"] == "desconocida"
        )
        assert mundo.envios_graph() == 1

        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
            )
        assert conc.status_code == 200, conc.text
        assert conc.json()["envio_estado"] == "confirmada" and conc.json()["encontrado"] is True
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert (fila["estado"], fila["envio_estado"]) == ("ejecutada", "confirmada")
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["conciliada_ejecutada"]
        assert mundo.envios_graph() == 1

    async def test_18_conciliacion_no_encontrado_permite_un_unico_reenvio(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "falla_antes"
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["estado_envio"] == "desconocida"
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
            )
        assert conc.json()["envio_estado"] == "sin_envio" and conc.json()["estado"] == "aprobada", conc.text
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["conciliada_no_ejecutada"]
        mundo.graph.modo = "202"
        ok = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert ok["success"] is True, ok
        assert len(mundo.graph.enviados) == 1
        segundo = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert segundo["success"] is False and segundo.get("ya_enviado")
        assert len(mundo.graph.enviados) == 1

    async def test_16_reinicio_del_runner_antes_y_despues_del_envio(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET state = 'abandoned', "
                    "lease_generation = lease_generation + 1 WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await s.commit()
        viejo = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert viejo["codigo"] == "intento_no_vigente", viejo
        nuevo = await ExecutionLeaseStore(mundo.maker, owner="runner-e2e-2").begin(task_id, str(uuid.uuid4()), 2, 600)
        plugin_nuevo = mundo.plugin_para(task_id, nuevo)
        ok = await asyncio.to_thread(mundo.enviar, plugin_nuevo, aprobacion_id=pedido["aprobacion_id"])
        assert ok["success"] is True
        async with mundo.maker() as s:
            await s.execute(
                text(
                    "UPDATE task_execution_attempts SET state = 'abandoned', "
                    "lease_generation = lease_generation + 1 WHERE attempt_id = :a"
                ),
                {"a": nuevo.attempt_id},
            )
            await s.commit()
        tercero = await ExecutionLeaseStore(mundo.maker, owner="runner-e2e-3").begin(task_id, str(uuid.uuid4()), 3, 600)
        repetido = await asyncio.to_thread(
            mundo.enviar, mundo.plugin_para(task_id, tercero), aprobacion_id=pedido["aprobacion_id"]
        )
        assert repetido["success"] is False and repetido.get("ya_enviado")
        assert mundo.envios_graph() == 1
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["attempt_id_ejecucion"] == nuevo.attempt_id

    async def test_reinicios_consecutivos_con_resultado_incierto_no_reenvian(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "timeout_tras_aceptar"
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["estado_envio"] == "desconocida"
        mundo.graph.modo = "202"
        for vuelta in range(3):
            mundo.backend.kill_group()
            await mundo.arrancar_backend(f"backend-reinicio-{vuelta}")
            otra_tarea, otro_lease = await mundo.tarea(mensaje="sí, reenvíalo")
            plugin_n = mundo.plugin_para(otra_tarea, otro_lease, mensaje="sí, reenvíalo")
            intento = await asyncio.to_thread(mundo.enviar, plugin_n, aprobacion_id=pedido["aprobacion_id"])
            assert intento["success"] is False, (vuelta, intento)
            assert mundo.envios_graph() == 1, vuelta
            assert [a["state"] for a in await mundo.acciones(task_id)] == ["iniciada"], vuelta
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
            )
        assert conc.status_code == 200, conc.text
        assert conc.json()["envio_estado"] == "confirmada"
        assert mundo.envios_graph() == 1

    async def test_caida_durante_la_conciliacion_no_habilita_un_reenvio(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "timeout_tras_aceptar"
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["estado_envio"] == "desconocida"
        mundo.graph.modo = "202"
        mundo.graph.falla_lectura_enviados = True
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            fallida = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
            )
        assert fallida.status_code == 502, fallida.text
        assert fallida.json()["detail"]["codigo"] == "enviados_no_disponibles"
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["iniciada"]
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["envio_estado"] == "desconocida"
        reintento = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert reintento["success"] is False
        assert mundo.envios_graph() == 1

        mundo.backend.kill_group()
        await mundo.arrancar_backend("backend-tras-conciliacion-fallida")
        mundo.graph.falla_lectura_enviados = False
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            conc = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
            )
        assert conc.status_code == 200, conc.text
        assert conc.json()["envio_estado"] == "confirmada" and conc.json()["encontrado"] is True
        assert [a["state"] for a in await mundo.acciones(task_id)] == ["conciliada_ejecutada"]
        assert mundo.envios_graph() == 1


class TestIdentidadYTokens:
    async def test_19_acceso_entre_propietarios(self, mundo):
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        ajena = await mundo.decidir(pedido["aprobacion_id"], usuario="otra-usuaria")
        assert ajena.status_code == 404
        headers = await _login_como(mundo.backend.base, "otra-usuaria")
        async with httpx.AsyncClient() as c:
            assert (
                await c.get(f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}", headers=headers)
            ).status_code == 404
            assert (
                await c.post(
                    f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/conciliar", headers=headers
                )
            ).status_code == 404
        await mundo.decidir(pedido["aprobacion_id"])
        tarea_otra, lease_otra = await mundo.tarea(usuario_id=mundo.otra_id, conversacion="conv-otra")
        plugin_otra = mundo.plugin_para(tarea_otra, lease_otra, usuario_id=mundo.otra_id)
        r = await asyncio.to_thread(mundo.enviar, plugin_otra, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is False and mundo.envios_graph() == 0

    async def test_20_el_plugin_no_obtiene_ni_usa_token_de_graph(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True
        assert mundo.graph.peticiones and all(p["auth"] == f"Bearer {TOKEN_SERVIDOR}" for p in mundo.graph.peticiones)
        log = Path(mundo.backend.log.name).read_text()
        assert "agent-token" not in log
        assert not any(k for k in ("HERMESHQ_GRAPH_TOKEN_SIMULADO",) if k in json.dumps(dict(__import__("os").environ)))

    async def test_agente_no_puede_aprobar_ni_reclamar_por_rutas_internas(self, mundo):
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        headers = plugin._cabeceras_control()
        base = f"{mundo.backend.base}/api/internal/control/aprobaciones/{pedido['aprobacion_id']}"
        async with httpx.AsyncClient() as c:
            for ruta in ("/aprobar", "/reclamar", "/resultado", "/decision"):
                r = await c.post(base + ruta, headers=headers, json={"task_id": task_id})
                assert r.status_code in (404, 405), (ruta, r.status_code)
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "pendiente"

    async def test_lectura_por_broker_con_la_url_base_por_defecto(self, mundo):
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        assert plugin.os.environ["HERMESHQ_INTERNAL_API_URL"].endswith("/api/internal")
        r = json.loads(await asyncio.to_thread(plugin._list_emails_tool, {"count": 3}))
        assert r["success"] is True, r
        assert [m["id"] for m in r["messages"]] == ["m1"]
        assert any("/mailFolders/inbox/messages" in p["ruta"] for p in mundo.graph.peticiones)


async def _modo(m: _Mundo, agent_id: str, modo: str):
    headers = await _login(m.backend.base)
    async with httpx.AsyncClient() as c:
        return await c.put(
            f"{m.backend.base}/api/agents/{agent_id}/aprobacion-central", headers=headers, json={"modo": modo}
        )


async def _token_directo(m: _Mundo, plugin) -> httpx.Response:
    headers = {k: v for k, v in plugin._cabeceras_control().items() if k != "Content-Type"}
    async with httpx.AsyncClient() as c:
        return await c.get(
            f"{m.backend.base}/api/internal/control/m365/agent-token", params={"user_id": m.admin_id}, headers=headers
        )


@pytest.fixture
async def mundo_transicion(db_engine, tmp_path, monkeypatch):
    engine, db_name = db_engine
    m = _Mundo(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        tokens=True,
    )
    try:
        await m.arrancar_backend()
        await m.sembrar()
        yield m
    finally:
        m.cerrar()


class TestEntregaDeTokensRetirada:
    async def test_ningun_agente_obtiene_token_por_ninguna_ruta(self, mundo):
        await _modo(mundo, mundo.agent_id, "desactivada")
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        cab = {k: v for k, v in plugin._cabeceras_control().items() if k != "Content-Type"}
        async with httpx.AsyncClient() as c:
            for ruta in ("/api/internal/control/m365/agent-token", "/api/m365/agent-token"):
                r = await c.get(f"{mundo.backend.base}{ruta}", params={"user_id": mundo.admin_id}, headers=cab)
                assert r.status_code == 410, (ruta, r.status_code, r.text)
                assert r.json()["detail"]["motivo"] == "entrega_de_tokens_retirada"
            modo = await c.get(
                f"{mundo.backend.base}/api/agents/{mundo.agent_id}/aprobacion-central",
                headers=await _login(mundo.backend.base),
            )
        assert modo.json()["modo"] == "desactivada" and modo.json()["efectiva"] is True

    async def test_las_operaciones_autorizadas_siguen_funcionando_sin_tokens(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert r["success"] is True
        lista = json.loads(await asyncio.to_thread(plugin._list_emails_tool, {"count": 2}))
        assert lista["success"] is True
        assert "agent-token" not in Path(mundo.backend.log.name).read_text()

    def test_el_runner_recibe_modo_central_cuando_no_hay_tokens(self, monkeypatch):
        from types import SimpleNamespace

        from hermeshq.services.aprobacion_central import central_activa_para

        ajustes = get_settings()
        monkeypatch.setattr(ajustes, "m365_agent_token_enabled", False)
        monkeypatch.setattr(ajustes, "approval_central_enabled", False)
        assert central_activa_para(SimpleNamespace(approval_central_mode="desactivada")) is True
        monkeypatch.setattr(ajustes, "m365_agent_token_enabled", True)
        assert central_activa_para(SimpleNamespace(approval_central_mode="desactivada")) is False
        assert central_activa_para(SimpleNamespace(approval_central_mode="activada")) is True


class TestConfiguracionPorAgente:
    async def test_modo_efectivo_y_precedencia(self, mundo_transicion):
        mundo = mundo_transicion
        r = await _modo(mundo, mundo.agent_id, "desactivada")
        assert r.status_code == 200 and r.json()["efectiva"] is False
        r = await _modo(mundo, mundo.agent_id, "heredar")
        assert r.json()["efectiva"] is True
        r = await _modo(mundo, mundo.agent_id, "activada")
        assert r.json()["efectiva"] is True

    async def test_agente_con_central_no_obtiene_token_ni_usa_ruta_legacy(self, mundo_transicion):
        mundo = mundo_transicion
        await _modo(mundo, mundo.agent_id, "activada")
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        r = await _token_directo(mundo, plugin)
        assert r.status_code == 403 and r.json()["detail"]["motivo"] == "aprobacion_central_activa"
        plugin.os.environ["HERMESHQ_APROBACION_CENTRAL"] = "0"
        plugin_legacy = _cargar_plugin()
        legado = json.loads(
            await asyncio.to_thread(
                plugin_legacy._send_email_tool,
                {"to": "destino@cliente.example", "subject": "x", "body": "y", "aprobado_por_el_usuario": True},
            )
        )
        assert legado["success"] is False and "aprobacion_central_activa" in json.dumps(legado, ensure_ascii=False)
        async with httpx.AsyncClient() as c:
            via_graph = await c.post(
                f"{mundo.backend.base}/api/internal/control/m365/graph",
                headers={k: v for k, v in plugin._cabeceras_control().items()},
                json={
                    "tarea_id": task_id,
                    "metodo": "POST",
                    "ruta": "/me/sendMail",
                    "cuerpo_json": {"message": {"subject": "x"}},
                },
            )
        assert via_graph.status_code == 403
        assert mundo.envios_graph() == 0

    async def test_agente_sin_central_usa_su_ruta_y_no_el_envio_central(self, mundo_transicion):
        mundo = mundo_transicion
        await _modo(mundo, mundo.agent_id, "desactivada")
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        r = await _token_directo(mundo, plugin)
        assert "aprobacion_central_activa" not in r.text
        async with httpx.AsyncClient() as c:
            envio = await c.post(
                f"{mundo.backend.base}/api/internal/control/m365/correo/enviar",
                headers=plugin._cabeceras_control(),
                json={"tarea_id": task_id, "aprobacion_id": "x", "contenido": {}},
            )
            crear = await c.post(
                f"{mundo.backend.base}/api/internal/control/aprobaciones",
                headers=plugin._cabeceras_control(),
                json={"task_id": task_id, "tipo": "correo.enviar", "contenido": {}},
            )
        assert envio.status_code == 404 and crear.status_code == 404

    async def test_desactivar_con_aprobacion_pendiente_la_expira_y_no_revive(self, mundo_transicion):
        mundo = mundo_transicion
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await _modo(mundo, mundo.agent_id, "desactivada")
        assert r.status_code == 200 and pedido["aprobacion_id"] in r.json()["expiradas"]
        await _modo(mundo, mundo.agent_id, "activada")
        reintento = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        assert reintento["success"] is False and reintento["requiere_aprobacion"] is True
        assert "expiró" in reintento["error"]
        assert mundo.envios_graph() == 0

    async def test_desactivar_con_envio_en_curso_se_rechaza(self, mundo_transicion):
        mundo = mundo_transicion
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "timeout_tras_aceptar"
        await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        r = await _modo(mundo, mundo.agent_id, "desactivada")
        assert r.status_code == 409 and r.json()["detail"]["codigo"] == "ejecucion_en_curso"
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            modo = await c.get(f"{mundo.backend.base}/api/agents/{mundo.agent_id}/aprobacion-central", headers=headers)
        assert modo.json()["efectiva"] is True

    async def test_acceso_cruzado_entre_agentes(self, mundo_transicion):
        mundo = mundo_transicion
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        async with mundo.maker() as s:
            otro = Agent(
                node_id=(await s.get(Agent, mundo.agent_id)).node_id,
                name="agente-b",
                slug="agente-b",
                status="running",
                workspace_path=str(mundo.workspace),
                approval_central_mode="activada",
            )
            s.add(otro)
            await s.flush()
            s.add(AgentAssignment(agent_id=otro.id, user_id=mundo.admin_id))
            await s.commit()
            otro_id = otro.id
        mundo.agent_id, original = otro_id, mundo.agent_id
        try:
            t_b, l_b = await mundo.tarea()
            plugin_b = mundo.plugin_para(t_b, l_b)
            r = await asyncio.to_thread(mundo.enviar, plugin_b, aprobacion_id=pedido["aprobacion_id"])
        finally:
            mundo.agent_id = original
        assert r["success"] is False and mundo.envios_graph() == 0
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "aprobada"

    async def test_solo_un_administrador_cambia_el_modo_y_el_agente_no_puede(self, mundo_transicion):
        mundo = mundo_transicion
        headers = await _login_como(mundo.backend.base, "otra-usuaria")
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{mundo.backend.base}/api/agents/{mundo.agent_id}/aprobacion-central",
                headers=headers,
                json={"modo": "desactivada"},
            )
        assert r.status_code in (401, 403)
        from hermeshq.schemas.agent import AgentCreate, AgentUpdate

        assert "approval_central_mode" not in AgentUpdate.model_fields
        assert "approval_central_mode" not in AgentCreate.model_fields
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{mundo.backend.base}/api/agents/{mundo.agent_id}/aprobacion-central",
                headers=plugin._cabeceras_control(),
                json={"modo": "desactivada"},
            )
        assert r.status_code in (401, 403)
        async with mundo.maker() as s:
            assert (await s.get(Agent, mundo.agent_id)).approval_central_mode == "heredar"
