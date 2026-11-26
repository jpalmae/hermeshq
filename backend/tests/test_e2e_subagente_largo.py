import json
import threading
import time
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

pytestmark = [pytest.mark.integration, requires_database]

LIMITE_INACTIVIDAD_S = 45
MARCA_SUBAGENTE = "focused subagent"


def _texto(mensaje: dict) -> str:
    contenido = mensaje.get("content")
    if isinstance(contenido, list):
        return " ".join(str(p.get("text") or "") for p in contenido if isinstance(p, dict))
    return str(contenido or "")


class _StubSubagente(_FakeLLM):
    retardo = 12.0
    rondas = 8
    colgar = 0.0
    archivo = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        mensajes = request.get("messages") or []
        sistema = " ".join(_texto(m) for m in mensajes if m.get("role") == "system")
        resultados = sum(1 for m in mensajes if m.get("role") == "tool")
        if MARCA_SUBAGENTE in sistema:
            type(self).llamadas_subagente.append(time.monotonic())
            time.sleep(self.colgar if (self.colgar and resultados == 0) else self.retardo)
            if resultados < self.rondas:
                self._responder(request, herramienta=("read_file", {"path": self.archivo}), id_=f"sub_{resultados}")
            else:
                self._responder(request, texto="Subtarea lista: archivo leído varias veces.")
        elif resultados == 0:
            self._responder(
                request,
                herramienta=("delegate_task", {"goal": "Lee el archivo datos.txt varias veces", "toolsets": ["file"]}),
                id_="delegar_1",
            )
        else:
            self._responder(request, texto="Listo: el subagente terminó su trabajo.")

    def _responder(self, request: dict, texto: str | None = None, herramienta=None, id_: str = "c"):
        llamada = None
        if herramienta:
            nombre, args = herramienta
            llamada = {"id": id_, "type": "function", "function": {"name": nombre, "arguments": json.dumps(args)}}
        fin = "tool_calls" if llamada else "stop"
        uso = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if request.get("stream"):
            base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
            delta = {"role": "assistant"}
            if llamada:
                delta["tool_calls"] = [{"index": 0, **llamada}]
            else:
                delta["content"] = texto
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
        mensaje = {"role": "assistant", "content": texto}
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
async def stack_subagente(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-subagente"
    workspace.mkdir(parents=True)
    archivo = workspace / "datos.txt"
    archivo.write_text("contenido para el subagente", encoding="utf-8")
    handler = type(
        "LLMSubagente",
        (_StubSubagente,),
        {"delay": 0.0, "requests": [], "llamadas_subagente": [], "archivo": str(archivo)},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-subagente", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-subagente",
            slug="agente-subagente",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file", "delegation"],
            max_iterations=6,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instance = _Instance(
        "instancia-subagente",
        _scratch_url(db_name),
        workspace,
        llm_url,
        tmp_path,
        extra_env={"TASK_IDLE_TIMEOUT_SECONDS": str(LIMITE_INACTIVIDAD_S)},
    )
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
            json={"agent_id": stack["agent_id"], "prompt": f"Delega la lectura del archivo {uuid.uuid4().hex[:6]}"},
        )
    )["id"]
    final = await _until(lambda: _final_status(instance.base, headers, task_id), 400, "fin de la tarea")
    async with stack["maker"]() as session:
        task = await session.get(Task, task_id)
    return final, task


class TestSubagenteLargo:
    async def test_subagente_que_trabaja_mas_que_el_limite_de_inactividad_termina(self, stack_subagente):
        handler = stack_subagente["handler"]
        final, task = await _ejecutar(stack_subagente)
        llamadas = handler.llamadas_subagente
        assert len(llamadas) == handler.rondas + 1, len(llamadas)
        assert llamadas[-1] - llamadas[0] > 2 * LIMITE_INACTIVIDAD_S
        assert final["status"] == "completed", (final.get("error_message"), task.metadata_json.get("end_reason"))
        assert "no output" not in (task.error_message or "")
        assert "terminó" in (task.response or "")

    async def test_una_llamada_colgada_mas_que_el_limite_se_sigue_deteniendo(self, stack_subagente):
        handler = stack_subagente["handler"]
        handler.colgar = LIMITE_INACTIVIDAD_S + 40
        final, task = await _ejecutar(stack_subagente)
        assert final["status"] == "failed"
        assert f"no output for {LIMITE_INACTIVIDAD_S}s" in (task.error_message or "") or (
            task.metadata_json.get("end_reason") in ("idle_timeout", "sin_salida")
        ), (task.error_message, task.metadata_json.get("end_reason"))
