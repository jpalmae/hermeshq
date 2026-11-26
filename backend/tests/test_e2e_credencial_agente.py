from __future__ import annotations

import json
import threading
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

CODIGO = (
    "import os, subprocess\n"
    "print('HIJO', sorted(k for k in os.environ if k.startswith('HERMESHQ_')))\n"
    "print('NIETO', subprocess.run(['sh','-c','env | grep ^HERMESHQ_ | cut -d= -f1'], capture_output=True, text=True).stdout.split())\n"
)


class _StubLlamador(_FakeLLM):
    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(largo) or b"{}")
        type(self).requests.append(pedido)
        mensajes = pedido.get("messages") or []
        resultados = [m for m in mensajes if m.get("role") == "tool"]
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        plan = [("hq_schedule_list", {}), ("execute_code", {"code": CODIGO})]
        if len(resultados) < len(plan) and pedido.get("tools"):
            nombre, args = plan[len(resultados)]
            llamada = {
                "index": 0,
                "id": f"call_{len(resultados)}",
                "type": "function",
                "function": {"name": nombre, "arguments": json.dumps(args)},
            }
            chunks = [
                {
                    **base,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "tool_calls": [llamada]}, "finish_reason": None}
                    ],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            chunks = [
                {
                    **base,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "Listo."}, "finish_reason": None}
                    ],
                },
                {
                    **base,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
            ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


async def test_el_runner_actua_con_el_token_de_su_intento(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-credencial"
    workspace.mkdir(parents=True)
    handler = type("LLMCredencial", (_StubLlamador,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as s:
        nodo = Node(name="nodo-credencial", hostname="localhost")
        s.add(nodo)
        await s.flush()
        agente = Agent(
            node_id=nodo.id,
            name="agente-credencial",
            slug="agente-credencial",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file", "code_execution", "hermeshq_schedule"],
            max_iterations=6,
        )
        s.add(agente)
        await s.commit()
        agent_id = agente.id
    inst = _Instance(
        "instancia-credencial",
        _scratch_url(db_name),
        workspace,
        llm_url,
        tmp_path,
        extra_env={"HQ_E2E_INSTALACION_REAL": "1", "AGENT_TOKEN_MODE": "strict"},
    )
    try:
        await inst.wait_ready()
        headers = await _login(inst.base)
        task_id = (
            await _api(
                "POST",
                inst.base,
                "/tasks",
                headers,
                json={"agent_id": agent_id, "prompt": "lista mis programaciones", "metadata": {"thread_user_id": None}},
            )
        )["id"]
        final = await _until(lambda: _final_status(inst.base, headers, task_id), 180, "tarea")
        assert final["status"] == "completed", final
        resultados = [m for m in handler.requests[-1]["messages"] if m.get("role") == "tool"]
        assert resultados, [m.get("role") for m in handler.requests[-1]["messages"]]
        print("RESULTADOS", [str(m.get("content"))[:300] for m in resultados])
        lista = json.loads(resultados[0]["content"])
        assert lista.get("success") is True, lista
        salida = str(resultados[1]["content"])
        assert "HIJO" in salida and "HERMESHQ_AGENT_TOKEN" not in salida and "HERMESHQ_TASK_PAYLOAD" not in salida, (
            salida
        )
        assert "HERMESHQ_ATTEMPT_ID" not in salida, salida
        async with maker() as s:
            tarea = await s.get(Task, task_id)
        assert tarea.status == "completed"
    finally:
        inst.kill_group()
        server.shutdown()
