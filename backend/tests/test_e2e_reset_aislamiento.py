import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import _api, _Instance, _login, _scratch_url
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.services.public_chat_service import _hash_key

pytestmark = [pytest.mark.integration, requires_database]

SECRETO_CHAT = "SECRETO-CHAT-41"
SECRETO_MEMORIA = "SECRETO-MEMORIA-77"


class _StubEco(_FakeLLM):
    delay_lenta = 0.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        usuario = [m for m in (request.get("messages") or []) if m.get("role") == "user"]
        ultimo = str((usuario[-1].get("content") if usuario else "") or "")
        if "LENTA" in ultimo and self.delay_lenta:
            import time as _time

            _time.sleep(self.delay_lenta)
        respuesta = f"ECO: {ultimo[-80:]}"
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        chunks = [
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": respuesta}, "finish_reason": None}],
            },
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
async def stack_reset(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-reset"
    hogar = workspace / ".hermes"
    (hogar / "memories").mkdir(parents=True)
    (hogar / "config.yaml").write_text(
        "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n", encoding="utf-8"
    )
    (hogar / "memories" / "MEMORY.md").write_text(
        f"Dato privado de otra conversación: {SECRETO_MEMORIA}\n", encoding="utf-8"
    )
    handler = type("LLMEco", (_StubEco,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-reset", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-reset",
            slug="agente-reset",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=[],
            max_iterations=2,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instances: list[_Instance] = []

    def spawn(name: str, extra_env: dict | None = None) -> _Instance:
        inst = _Instance(name, _scratch_url(db_name), workspace, llm_url, tmp_path, extra_env=extra_env)
        instances.append(inst)
        return inst

    try:
        yield {
            "maker": maker,
            "handler": handler,
            "agent_id": agent_id,
            "spawn": spawn,
            "workspace": workspace,
            "llm_url": llm_url,
        }
    finally:
        for inst in instances:
            inst.kill_group()
        server.shutdown()


async def _clave_publica(maker, agent_id: str, user_id: str | None = None) -> str:
    clave = f"hqk-{uuid.uuid4().hex}"
    async with maker() as s:
        if user_id is None:
            user_id = (await s.execute(text("SELECT id FROM users WHERE username = 'admin'"))).scalar_one()
        s.add(
            PublicChatApiKey(
                key_hash=_hash_key(clave),
                key_prefix=clave[:8],
                label="pruebas-reset",
                agent_id=agent_id,
                allowed_domains=["*"],
                is_active=True,
                bound_user_id=user_id,
                trust_sender_id=False,
            )
        )
        await s.commit()
    return clave


async def _segundo_usuario(maker, agent_id: str) -> str:
    from hermeshq.core.security import hash_password
    from hermeshq.models.agent_assignment import AgentAssignment
    from hermeshq.models.user import User

    async with maker() as s:
        usuario = User(
            username=f"usuario-b-{uuid.uuid4().hex[:6]}",
            display_name="Usuario B",
            password_hash=hash_password("clave-solo-pruebas"),
            role="user",
        )
        s.add(usuario)
        await s.flush()
        s.add(AgentAssignment(user_id=usuario.id, agent_id=agent_id))
        await s.commit()
        return usuario.id


async def _sesion(base: str, clave: str, remitente: str) -> tuple[str, str]:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{base}/api/public/chat/sessions", headers={"X-Api-Key": clave}, json={"sender_id": remitente}
        )
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["session_token"]


async def _mensaje(base: str, sesion: str, token: str, contenido: str) -> dict:
    async with (
        httpx.AsyncClient(timeout=180) as c,
        c.stream(
            "POST",
            f"{base}/api/public/chat/sessions/{sesion}/messages",
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


def _peticion_con(handler, marca: str) -> dict:
    encontradas = [r for r in handler.requests if marca in json.dumps(r, ensure_ascii=False)]
    assert encontradas, f"el modelo nunca recibió {marca}"
    return encontradas[-1]


async def _tarea_con(maker, marca: str) -> Task:
    async with maker() as s:
        filas = (await s.execute(select(Task).where(Task.prompt.contains(marca)))).scalars().all()
    assert filas, marca
    return filas[-1]


class TestResetConReinicioReal:
    async def test_reset_aisla_historial_memoria_y_epoca_y_conserva_crons(self, stack_reset):
        maker, handler, agent_id = stack_reset["maker"], stack_reset["handler"], stack_reset["agent_id"]
        a = stack_reset["spawn"]("instancia-reset-a")
        await a.wait_ready()
        clave = await _clave_publica(maker, agent_id)
        proxima = datetime.now(UTC) + timedelta(days=30)
        async with maker() as s:
            cron = ScheduledTask(
                agent_id=agent_id,
                name="informe-semanal",
                cron_expression="0 9 * * 1",
                prompt="Genera el informe semanal en tabla",
                enabled=True,
                next_run=proxima,
                timezone="America/Santiago",
            )
            s.add(cron)
            await s.commit()
            cron_id = cron.id

        sesion, token = await _sesion(a.base, clave, "aad-usuario-a")
        primera = await _mensaje(a.base, sesion, token, f"Mi código es {SECRETO_CHAT}. MARCA-ANTES")
        assert primera["type"] == "done", primera
        assert SECRETO_MEMORIA not in json.dumps(_peticion_con(handler, "MARCA-ANTES"), ensure_ascii=False)

        reinicio = await _mensaje(a.base, sesion, token, "/reset")
        assert reinicio["type"] == "done", reinicio
        despues = await _mensaje(a.base, sesion, token, "¿Cuál era mi código? PREGUNTA-1")
        assert despues["type"] == "done", despues
        contexto = json.dumps(_peticion_con(handler, "PREGUNTA-1"), ensure_ascii=False)
        assert SECRETO_CHAT not in contexto and SECRETO_MEMORIA not in contexto and "MARCA-ANTES" not in contexto
        assert (await _tarea_con(maker, "MARCA-ANTES")).metadata_json.get("conversation_epoch") == 0
        assert (await _tarea_con(maker, "PREGUNTA-1")).metadata_json.get("conversation_epoch") == 1

        a.kill_group()
        b = stack_reset["spawn"]("instancia-reset-b")
        await b.wait_ready()
        tras_reinicio = await _mensaje(b.base, sesion, token, "Repite mi código. PREGUNTA-2")
        assert tras_reinicio["type"] == "done", tras_reinicio
        contexto = json.dumps(_peticion_con(handler, "PREGUNTA-2"), ensure_ascii=False)
        assert SECRETO_CHAT not in contexto and SECRETO_MEMORIA not in contexto and "MARCA-ANTES" not in contexto
        assert "PREGUNTA-1" in contexto, "la conversación posterior al reset debe conservar su propio historial"
        assert (await _tarea_con(maker, "PREGUNTA-2")).metadata_json.get("conversation_epoch") == 1

        async with maker() as s:
            fila = await s.get(ScheduledTask, cron_id)
        assert fila is not None and fila.enabled is True
        assert (fila.cron_expression, fila.prompt, fila.timezone) == (
            "0 9 * * 1",
            "Genera el informe semanal en tabla",
            "America/Santiago",
        )
        assert fila.next_run == proxima

        headers = await _login(b.base)
        await _api(
            "POST", b.base, "/tasks", headers, json={"agent_id": agent_id, "prompt": "Tarea directa MARCA-DIRECTA"}
        )
        await _api("POST", b.base, f"/scheduled-tasks/{cron_id}/run", headers)
        for _ in range(120):
            vistos = json.dumps(handler.requests, ensure_ascii=False)
            if "MARCA-DIRECTA" in vistos and "Genera el informe semanal en tabla" in vistos:
                break
            await asyncio.sleep(0.5)
        assert SECRETO_MEMORIA not in json.dumps(_peticion_con(handler, "MARCA-DIRECTA"), ensure_ascii=False), (
            "una tarea directa a nombre de un usuario no carga la memoria compartida del agente"
        )
        assert SECRETO_MEMORIA in json.dumps(
            _peticion_con(handler, "Genera el informe semanal en tabla"), ensure_ascii=False
        )


def _contexto(handler, marca: str) -> str:
    return json.dumps(_peticion_con(handler, marca), ensure_ascii=False)


class TestIdentidadesYEpocas:
    async def test_dos_usuarios_del_mismo_agente_no_comparten_contexto_ni_archivos(self, stack_reset):
        maker, handler, agent_id = stack_reset["maker"], stack_reset["handler"], stack_reset["agent_id"]
        descargas = stack_reset["workspace"] / "sharepoint_downloads"
        descargas.mkdir(exist_ok=True)
        (descargas / "privado-usuario-a.xlsx").write_bytes(b"x")
        a = stack_reset["spawn"]("instancia-identidades")
        await a.wait_ready()
        clave_a = await _clave_publica(maker, agent_id)
        clave_b = await _clave_publica(maker, agent_id, await _segundo_usuario(maker, agent_id))
        sa, ta = await _sesion(a.base, clave_a, "aad-usuario-a")
        sb, tb = await _sesion(a.base, clave_b, "aad-usuario-b")

        ra = await _mensaje(
            a.base, sa, ta, "Revisa sharepoint_downloads/privado-usuario-a.xlsx; mi clave es CLAVE-USUARIO-A. MARCA-A"
        )
        assert ra["type"] == "done", ra
        rb = await _mensaje(a.base, sb, tb, "¿Qué archivos y claves conoces? MARCA-B")
        assert rb["type"] == "done", rb
        contexto_b = _contexto(handler, "MARCA-B")
        assert "CLAVE-USUARIO-A" not in contexto_b and "privado-usuario-a.xlsx" not in contexto_b

        simultaneas = await asyncio.gather(
            _mensaje(a.base, sa, ta, "Continúa MARCA-A2"), _mensaje(a.base, sb, tb, "Y ahora MARCA-B2")
        )
        assert [r["type"] for r in simultaneas] == ["done", "done"], simultaneas
        contexto_a2, contexto_b2 = _contexto(handler, "MARCA-A2"), _contexto(handler, "MARCA-B2")
        assert "CLAVE-USUARIO-A" in contexto_a2 and "privado-usuario-a.xlsx" in contexto_a2
        assert "CLAVE-USUARIO-A" not in contexto_b2 and "privado-usuario-a.xlsx" not in contexto_b2
        assert "MARCA-B" in contexto_b2 and "MARCA-A" not in contexto_b2

    async def test_tarea_anterior_al_reset_no_contamina_la_epoca_nueva(self, stack_reset):
        maker, handler, agent_id = stack_reset["maker"], stack_reset["handler"], stack_reset["agent_id"]
        handler.delay_lenta = 10.0
        a = stack_reset["spawn"]("instancia-en-vuelo")
        await a.wait_ready()
        sesion, token = await _sesion(a.base, await _clave_publica(maker, agent_id), "aad-en-vuelo")
        lenta = asyncio.create_task(_mensaje(a.base, sesion, token, "Trabajo largo LENTA DATO-VIEJO-9"))
        for _ in range(120):
            if any("DATO-VIEJO-9" in json.dumps(r, ensure_ascii=False) for r in handler.requests):
                break
            await asyncio.sleep(0.5)
        reinicio = await _mensaje(a.base, sesion, token, "/reset")
        assert reinicio["type"] == "done", reinicio
        nueva = await _mensaje(a.base, sesion, token, "Hola de nuevo PREGUNTA-NUEVA-1")
        assert nueva["type"] == "done", nueva
        assert "DATO-VIEJO-9" not in _contexto(handler, "PREGUNTA-NUEVA-1")

        await asyncio.sleep(handler.delay_lenta + 3)
        resultado_lento = await asyncio.wait_for(lenta, 30)
        assert "cancelada" in resultado_lento["content"] and "DATO-VIEJO-9" not in resultado_lento["content"]
        otra = await _mensaje(a.base, sesion, token, "Sigue PREGUNTA-NUEVA-2")
        assert otra["type"] == "done", otra
        contexto = _contexto(handler, "PREGUNTA-NUEVA-2")
        assert "DATO-VIEJO-9" not in contexto and "PREGUNTA-NUEVA-1" in contexto
        vieja = await _tarea_con(maker, "DATO-VIEJO-9")
        assert vieja.status == "cancelled", vieja.status
        assert (await _tarea_con(maker, "PREGUNTA-NUEVA-2")).metadata_json.get("conversation_epoch") == 1
