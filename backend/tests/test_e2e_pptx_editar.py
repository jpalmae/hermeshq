import hashlib
import json
import shutil
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import _api, _final_status, _Instance, _login, _scratch_url, _until
from test_execution_lease import _FakeLLM
from test_pptx_editar import _crear_presentacion

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"


class _StubHerramienta(_FakeLLM):
    herramienta = ""
    argumentos: dict = {}

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(largo) or b"{}")
        type(self).requests.append(pedido)
        hay_resultado = any(m.get("role") == "tool" for m in pedido.get("messages") or [])
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        if not hay_resultado:
            llamada = {
                "index": 0,
                "id": "call_pptx",
                "type": "function",
                "function": {"name": self.herramienta, "arguments": json.dumps(self.argumentos)},
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
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Listo, versión nueva creada."},
                            "finish_reason": None,
                        }
                    ],
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
async def stack_pptx(db_engine, tmp_path):
    pytest.importorskip("pptx")
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-pptx"
    (workspace / "uploads").mkdir(parents=True)
    hogar = workspace / ".hermes"
    shutil.copytree(PLUGIN, hogar / "plugins" / "hermeshq_pptx")
    (hogar / "config.yaml").write_text("plugins:\n  enabled:\n  - hermeshq_pptx\n", encoding="utf-8")
    handler = type("LLMPptx", (_StubHerramienta,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-pptx", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-pptx",
            slug="agente-pptx",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["hermeshq_pptx"],
            max_iterations=4,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instancias: list[_Instance] = []

    def spawn(nombre: str) -> _Instance:
        inst = _Instance(nombre, _scratch_url(db_name), workspace, llm_url, tmp_path)
        instancias.append(inst)
        return inst

    try:
        yield {"maker": maker, "handler": handler, "agent_id": agent_id, "spawn": spawn, "workspace": workspace}
    finally:
        for inst in instancias:
            inst.kill_group()
        server.shutdown()


def _resultado_de_herramienta(handler) -> dict:
    for pedido in handler.requests:
        for mensaje in pedido.get("messages") or []:
            if mensaje.get("role") == "tool":
                return json.loads(mensaje.get("content") or "{}")
    raise AssertionError("la herramienta no devolvió resultado al modelo")


class TestEditarPptxPorLaRutaReal:
    async def test_el_agente_edita_un_pptx_subido_sin_destruirlo_y_lo_entrega(self, stack_pptx):
        original = stack_pptx["workspace"] / "uploads" / "informe.pptx"
        _crear_presentacion(original)
        sha_original = hashlib.sha256(original.read_bytes()).hexdigest()
        handler = stack_pptx["handler"]
        handler.herramienta = "hq_pptx_editar"
        handler.argumentos = {
            "path": "uploads/informe.pptx",
            "cambios": [{"reemplazar": {"texto": "Orion", "por": "Atlas", "lamina": 2}}],
        }
        a = stack_pptx["spawn"]("instancia-pptx")
        await a.wait_ready()
        headers = await _login(a.base)
        task_id = (
            await _api(
                "POST",
                a.base,
                "/tasks",
                headers,
                json={"agent_id": stack_pptx["agent_id"], "prompt": f"Cambia Orion por Atlas {uuid.uuid4().hex[:6]}"},
            )
        )["id"]
        final = await _until(lambda: _final_status(a.base, headers, task_id), 120, "fin de la tarea")
        assert final["status"] == "completed", final

        resultado = _resultado_de_herramienta(handler)
        assert resultado["success"] is True, resultado
        assert resultado["path"] == "uploads/informe_v2.pptx"
        assert resultado["comparacion"]["laminas_antes"] == resultado["comparacion"]["laminas_despues"] == 3
        assert hashlib.sha256(original.read_bytes()).hexdigest() == sha_original
        from pptx import Presentation

        nueva = Presentation(str(stack_pptx["workspace"] / "uploads" / "informe_v2.pptx"))
        assert [s.shapes.title.text for s in nueva.slides] == [
            "Informe Orion 2026",
            "Resultados del trimestre",
            "Tendencia",
        ]
        async with stack_pptx["maker"]() as session:
            tarea = await session.get(Task, task_id)
        adjuntos = (tarea.metadata_json or {}).get("response_attachments") or []
        assert any(adj.get("filename", "").endswith(".pptx") for adj in adjuntos), (tarea.response, adjuntos)
        assert "MEDIA: uploads/" in (tarea.response or "")
