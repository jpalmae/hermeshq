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


class _StubEntrega(_FakeLLM):
    texto = ""

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        type(self).requests.append(json.loads(self.rfile.read(largo) or b"{}"))
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        chunks = [
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": self.texto}, "finish_reason": None}],
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


async def test_adjuntos_invalidos_no_se_entregan_y_se_avisa(db_engine, tmp_path):
    pptx = pytest.importorskip("pptx")
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-adjuntos"
    salida = workspace / "outputs"
    salida.mkdir(parents=True)
    (salida / "informe.docx").write_bytes(b"<html><body><table><tr><td>no soy word</td></tr></table></body></html>")
    (salida / "resumen").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    prs = pptx.Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Portada"
    prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(salida / "deck.pptx"))
    handler = type(
        "LLMEntrega",
        (_StubEntrega,),
        {
            "delay": 0.0,
            "requests": [],
            "texto": (
                "Listo, aquí van los archivos.\nMEDIA:outputs/informe.docx\nMEDIA:outputs/resumen\nMEDIA:outputs/deck.pptx"
            ),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as s:
        nodo = Node(name="nodo-adjuntos", hostname="localhost")
        s.add(nodo)
        await s.flush()
        agente = Agent(
            node_id=nodo.id,
            name="agente-adjuntos",
            slug="agente-adjuntos",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file"],
            max_iterations=3,
        )
        s.add(agente)
        await s.commit()
        agent_id = agente.id
    inst = _Instance("instancia-adjuntos", _scratch_url(db_name), workspace, llm_url, tmp_path)
    try:
        await inst.wait_ready()
        headers = await _login(inst.base)
        task_id = (
            await _api(
                "POST", inst.base, "/tasks", headers, json={"agent_id": agent_id, "prompt": "Entrégame los archivos"}
            )
        )["id"]
        final = await _until(lambda: _final_status(inst.base, headers, task_id), 120, "fin de la tarea")
        assert final["status"] == "completed", final
        async with maker() as s:
            tarea = await s.get(Task, task_id)
        adjuntos = (tarea.metadata_json or {}).get("response_attachments") or []
        nombres = sorted(a["filename"] for a in adjuntos)
        assert nombres == ["deck.pptx", "resumen.pdf"], (nombres, tarea.response)
        assert "No adjunté informe.docx" in tarea.response and "no es un archivo de Office" in tarea.response
        assert "la lámina 2 está vacía" in tarea.response
        assert tarea.response.count("MEDIA: uploads/") == 2
    finally:
        inst.kill_group()
        server.shutdown()
