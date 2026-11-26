import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
pytest.importorskip("run_agent")

PLUGIN_INIT = """
import json


def _handler(args, **kw):
    return json.dumps({"success": True, "hits": ["matriz.xlsx"]})


def register(ctx):
    ctx.register_tool(
        name="sharepoint_search_prueba",
        toolset="hermeshq_sharepoint_prueba",
        schema={"name": "sharepoint_search_prueba", "description": "Busca en SharePoint (prueba)",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}},
        handler=_handler,
        check_fn=lambda: True,
        description="Busca en SharePoint (prueba)",
    )
"""


class _LLM(BaseHTTPRequestHandler):
    visto: list = []
    nombre_llamado = "sharepoint_search_prueba"

    def log_message(self, *a):
        pass

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(largo) or b"{}")
        type(self).visto.append(pedido)
        mensajes = pedido.get("messages") or []
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
        if not any(m.get("role") == "tool" for m in mensajes):
            llamada = {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": type(self).nombre_llamado, "arguments": json.dumps({"query": "matriz"})},
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
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def llm():
    _LLM.visto = []
    _LLM.nombre_llamado = "sharepoint_search_prueba"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def _correr(tmp_path, base_url, diferidos):
    home = tmp_path / "home"
    plugin = home / "plugins" / "hermeshq_sharepoint_prueba"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: hermeshq_sharepoint_prueba\nversion: '1.0.0'\ndescription: prueba\nprovides_tools:\n  - sharepoint_search_prueba\n",
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(PLUGIN_INIT, encoding="utf-8")
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermeshq_sharepoint_prueba\n", encoding="utf-8")
    payload = {
        "task_id": "t1",
        "prompt": "Dime qué dice la matriz de priorización",
        "model": "m",
        "provider": "custom",
        "base_url": base_url,
        "api_key": "x",
        "hermes_home": str(home),
        "cwd": str(tmp_path),
        "enabled_toolsets": ["file"],
        "toolsets_diferidos": diferidos,
        "max_iterations": 6,
        "metadata": {"source": "background_job"},
        "conversation_history": [],
    }
    r = subprocess.run(
        [sys.executable, str(RAIZ / "scripts" / "hermes_task_runner.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": str(RAIZ.parent), "HOME": str(tmp_path)},
    )
    eventos = [json.loads(linea) for linea in r.stdout.splitlines() if linea.startswith("{")]
    return r, eventos


def _resultados_de_herramienta(eventos):
    resultado = next(e for e in eventos if e.get("event") == "result")
    return [str(m.get("content")) for m in resultado["messages"] if m.get("role") == "tool"], resultado


def test_la_herramienta_de_un_toolset_diferido_se_activa_y_se_ejecuta(tmp_path, llm):
    r, eventos = _correr(tmp_path, llm, ["hermeshq_sharepoint_prueba"])
    resultados, resultado = _resultados_de_herramienta(eventos)
    assert r.returncode == 0, (r.stdout[-800:], r.stderr[-800:])
    assert resultados and not any("does not exist" in x for x in resultados), resultados
    assert any("matriz.xlsx" in x for x in resultados), resultados
    activados = [e for e in eventos if e.get("event") == "tool_activated"]
    assert [e["name"] for e in activados] == ["sharepoint_search_prueba"]
    assert resultado["final_response"].strip() == "Listo."


def test_control_negativo_sin_toolsets_diferidos_sigue_sin_existir(tmp_path, llm):
    r, eventos = _correr(tmp_path, llm, [])
    resultados, _ = _resultados_de_herramienta(eventos)
    assert any("does not exist" in x for x in resultados), resultados
    assert not [e for e in eventos if e.get("event") == "tool_activated"]


def test_un_toolset_diferido_distinto_no_activa_la_herramienta(tmp_path, llm):
    r, eventos = _correr(tmp_path, llm, ["hermeshq_ms365_mail"])
    resultados, _ = _resultados_de_herramienta(eventos)
    assert any("does not exist" in x for x in resultados), resultados
