import json
import threading
import uuid
from http.server import ThreadingHTTPServer

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import _api, _final_status, _Instance, _login, _scratch_url, _until
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.services.schedule_notifications import friendly_error_text

pytestmark = [pytest.mark.integration, requires_database]


class _StubVacio(_FakeLLM):
    leer_primero = False
    archivo = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        resultados = sum(1 for m in (request.get("messages") or []) if m.get("role") == "tool")
        if self.leer_primero and resultados == 0:
            self._responder(request, herramienta=("read_file", {"path": self.archivo}))
        else:
            self._responder(request)

    def _responder(self, request: dict, herramienta=None):
        llamada = None
        if herramienta:
            nombre, args = herramienta
            llamada = {"id": "leer_1", "type": "function", "function": {"name": nombre, "arguments": json.dumps(args)}}
        fin = "tool_calls" if llamada else "stop"
        uso = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
        if request.get("stream"):
            base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
            delta = {"role": "assistant"}
            if llamada:
                delta["tool_calls"] = [{"index": 0, **llamada}]
            else:
                delta["content"] = ""
            chunks = [
                {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": fin}], "usage": uso},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        mensaje = {"role": "assistant", "content": ""}
        if llamada:
            mensaje["tool_calls"] = [llamada]
        cuerpo = json.dumps(
            {
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "modelo-simulado",
                "choices": [{"index": 0, "message": mensaje, "finish_reason": fin}],
                "usage": uso,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


@pytest.fixture
async def stack_vacio(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-vacio"
    workspace.mkdir(parents=True)
    archivo = workspace / "datos.txt"
    archivo.write_text("contenido para leer", encoding="utf-8")
    handler = type("LLMVacio", (_StubVacio,), {"delay": 0.0, "requests": [], "archivo": str(archivo)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-vacio", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-vacio",
            slug="agente-vacio",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file"],
            max_iterations=6,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instance = _Instance("instancia-vacio", _scratch_url(db_name), workspace, llm_url, tmp_path)
    try:
        await instance.wait_ready()
        yield {"maker": maker, "handler": handler, "agent_id": agent_id, "instance": instance}
    finally:
        instance.kill_group()
        server.shutdown()


async def _ejecutar(stack) -> tuple[dict, Task]:
    instance = stack["instance"]
    headers = await _login(instance.base)
    task_id = (
        await _api(
            "POST",
            instance.base,
            "/tasks",
            headers,
            json={"agent_id": stack["agent_id"], "prompt": f"Arma el resumen {uuid.uuid4().hex[:6]}"},
        )
    )["id"]
    final = await _until(lambda: _final_status(instance.base, headers, task_id), 300, "fin de la tarea")
    async with stack["maker"]() as session:
        task = await session.get(Task, task_id)
    return final, task


class TestRespuestaVaciaPorLaRutaReal:
    async def test_tres_respuestas_vacias_sin_herramientas_fallan_y_no_entregan_el_centinela(self, stack_vacio):
        final, task = await _ejecutar(stack_vacio)
        assert final["status"] == "failed", (final.get("status"), task.response, task.error_message)
        assert "empty_response_exhausted" in (task.error_message or ""), task.error_message
        assert "(empty)" not in (task.response or "")
        texto = friendly_error_text(task.error_message)
        assert "no devolvió respuesta" in texto and "llamada inválida" not in texto

    async def test_con_una_herramienta_ya_ejecutada_el_aviso_la_nombra(self, stack_vacio):
        stack_vacio["handler"].leer_primero = True
        final, task = await _ejecutar(stack_vacio)
        assert final["status"] == "completed", (
            final.get("status"),
            task.error_message,
            [
                (m.get("role"), str(m.get("content"))[:60], bool(m.get("tool_calls")))
                for m in (task.messages_json or [])
            ],
            task.metadata_json.get("partial"),
        )
        assert "(empty)" not in (task.response or ""), task.response
        assert "read_file" in (task.response or ""), task.response
        assert "No sé si el resultado quedó completo" in (task.response or "")
