from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from conftest import requires_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import ADMIN_PASSWORD, _api, _final_status, _Instance, _login, _scratch_url, _until
from test_execution_lease import _FakeLLM

from hermeshq.core.security import hash_password
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.user import User

pytestmark = [pytest.mark.integration, requires_database]


class _StubDelegador(_FakeLLM):
    destinos: list = []

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(largo) or b"{}")
        type(self).requests.append(pedido)
        mensajes = pedido.get("messages") or []
        hechos = sum(1 for m in mensajes if m.get("role") == "tool")
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        texto_usuario = " ".join(str(m.get("content") or "") for m in mensajes if m.get("role") == "user")
        if "DELEGA_A" in texto_usuario and hechos < len(self.destinos) and pedido.get("tools"):
            args = {"target_agent": self.destinos[hechos], "instruction": "Prepara el resumen semanal."}
            llamada = {
                "index": 0,
                "id": f"call_{hechos}",
                "type": "function",
                "function": {"name": "hq_delegate_task", "arguments": json.dumps(args)},
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


async def test_delegar_en_el_agente_de_otra_usuaria_se_bloquea(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    raiz = tmp_path / "workspaces"
    agentes = {}
    async with maker() as s:
        u1 = User(username="usuaria-uno", display_name="U1", password_hash=hash_password(ADMIN_PASSWORD))
        u2 = User(username="usuaria-dos", display_name="U2", password_hash=hash_password(ADMIN_PASSWORD))
        nodo = Node(name="nodo-delegacion", hostname="localhost")
        s.add_all([u1, u2, nodo])
        await s.flush()
        for clave, dueno in (("a", u1), ("b", u2), ("c", u1)):
            ws = raiz / f"agente-{clave}"
            ws.mkdir(parents=True)
            ag = Agent(
                node_id=nodo.id,
                name=f"agente-{clave}",
                slug=f"agente-{clave}",
                workspace_path=str(ws),
                status="running",
                model="modelo-simulado",
                enabled_toolsets=["hermeshq_comms"],
                max_iterations=5,
                can_send_tasks=True,
                can_receive_tasks=True,
            )
            s.add(ag)
            await s.flush()
            s.add(AgentAssignment(agent_id=ag.id, user_id=dueno.id))
            agentes[clave] = ag.id
        await s.commit()
        u1_id = u1.id
    handler = type(
        "LLMDelegacion", (_StubDelegador,), {"delay": 0.0, "requests": [], "destinos": ["agente-b", "agente-c"]}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    inst = _Instance(
        "instancia-delegacion",
        _scratch_url(db_name),
        raiz / "agente-a",
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
                json={
                    "agent_id": agentes["a"],
                    "prompt": "DELEGA_A otros agentes el resumen",
                    "metadata": {"thread_user_id": u1_id},
                },
            )
        )["id"]
        final = await _until(lambda: _final_status(inst.base, headers, task_id), 180, "tarea de A")
        assert final["status"] == "completed", final
        resultados = [m for m in handler.requests[-1]["messages"] if m.get("role") == "tool"]
        hacia_b, hacia_c = (json.loads(r["content"]) for r in resultados[:2])
        assert not hacia_b.get("success"), hacia_b
        assert hacia_c.get("success") is True, hacia_c
        async with maker() as s:
            creadas = (
                (await s.execute(select(Task).where(Task.agent_id.in_([agentes["b"], agentes["c"]])))).scalars().all()
            )
        assert [t.agent_id for t in creadas] == [agentes["c"]], "solo se creó la tarea del agente de la misma usuaria"
    finally:
        inst.kill_group()
        server.shutdown()
