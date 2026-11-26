import asyncio
import json
import re
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import (
    _api,
    _async_true,
    _attempts,
    _final_status,
    _Instance,
    _login,
    _scratch_url,
    _until,
)
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

PATRON_LECTURA = re.compile(r"LEER=(\S+)")
PATRON_HERRAMIENTA = re.compile(r"HERR=(\w+)")


class _StubConHerramientas(_FakeLLM):
    delay_tras_herramienta = 0.0
    silencio_corte = 40.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        mensajes = request.get("messages") or []
        hay_resultado = any(m.get("role") == "tool" for m in mensajes)
        texto = " ".join(str(m.get("content") or "") for m in mensajes if m.get("role") == "user")
        ruta = PATRON_LECTURA.search(texto)
        pedida = PATRON_HERRAMIENTA.search(texto)
        herramienta = pedida.group(1) if pedida else "read_file"
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        if "CORTE_STREAM" in texto and not hay_resultado:
            inicio = {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_cortado",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"pa'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            blanco = {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "\n\n"}, "finish_reason": None}],
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(blanco)}\n\n".encode())
            self.wfile.write(f"data: {json.dumps(inicio)}\n\n".encode())
            self.wfile.flush()
            time.sleep(self.silencio_corte)
            self.close_connection = True
            return
        if ruta and not hay_resultado:
            chunks = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_leer_1",
                                        "type": "function",
                                        "function": {"name": herramienta, "arguments": ""},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": json.dumps(
                                                {"path": ruta.group(1)}
                                                if herramienta == "read_file"
                                                else {"action": "delete", "name": "skill-inexistente"}
                                            )
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            if hay_resultado and self.delay_tras_herramienta:
                time.sleep(self.delay_tras_herramienta)
            chunks = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Listo: contenido recibido."},
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
async def stack_herramientas(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-herramientas"
    workspace.mkdir(parents=True)
    handler = type("LLMHerramientas", (_StubConHerramientas,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-herramientas", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-herramientas",
            slug="agente-herramientas",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file", "skills"],
            max_iterations=4,
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


async def _herramientas_registradas(maker, task_id: str, minimo: int) -> bool:
    rows = await _attempts(maker, task_id)
    return any(int(r["tool_events"] or 0) >= minimo for r in rows)


async def _inicios_registrados(maker, task_id: str) -> list[tuple[str, str]]:
    async with maker() as session:
        filas = await session.execute(
            text(
                "SELECT details->>'attempt_id', message FROM activity_logs "
                "WHERE task_id = :t AND event_type = 'task.tool_started' ORDER BY created_at"
            ),
            {"t": task_id},
        )
        return [(str(a), str(m)) for a, m in filas.all()]


async def _caida_tras_la_herramienta(stack, prompt_extra: str) -> tuple[str, dict]:
    maker, handler = stack["maker"], stack["handler"]
    handler.delay_tras_herramienta = 15.0
    archivo = stack["workspace"] / "datos.txt"
    archivo.write_text("contenido-unico-herramienta", encoding="utf-8")
    a = stack["spawn"]("instancia-herr-a")
    await a.wait_ready()
    headers = await _login(a.base)
    marca = f"marca-{uuid.uuid4().hex[:8]}"
    task_id = (
        await _api(
            "POST",
            a.base,
            "/tasks",
            headers,
            json={"agent_id": stack["agent_id"], "prompt": f"LEER={archivo} {prompt_extra} {marca}"},
        )
    )["id"]
    await _until(lambda: _herramientas_registradas(maker, task_id, 1), 90, "herramienta registrada")
    await _until(lambda: _async_true(len(handler.requests) >= 2), 90, "petición posterior a la herramienta")

    a.crash_server_only()
    b = stack["spawn"]("instancia-herr-b")
    await b.wait_ready()
    headers_b = await _login(b.base)
    final = await _until(lambda: _final_status(b.base, headers_b, task_id), 120, "recuperación")
    return task_id, final


class TestCaidaDespuesDeHerramienta:
    async def test_caida_tras_una_lectura_se_reintenta_una_vez(self, stack_herramientas):
        maker, handler = stack_herramientas["maker"], stack_herramientas["handler"]
        task_id, final = await _caida_tras_la_herramienta(stack_herramientas, "")

        assert final["status"] == "completed"
        rows = await _attempts(maker, task_id)
        assert [r["reconciliation"] for r in rows][0] == "interrumpida"
        assert len(rows) == 2
        inicios = await _inicios_registrados(maker, task_id)
        assert (rows[0]["attempt_id"], "read_file") in inicios
        assert (rows[1]["attempt_id"], "read_file") in inicios
        assert len(handler.requests) == 4
        async with maker() as session:
            db_task = await session.get(Task, task_id)
        assert db_task.metadata_json["recovery_reintentos"] == 1

    async def test_caida_tras_una_herramienta_con_efectos_no_se_reintenta(self, stack_herramientas):
        maker, handler = stack_herramientas["maker"], stack_herramientas["handler"]
        task_id, final = await _caida_tras_la_herramienta(stack_herramientas, "HERR=skill_manage")

        assert final["status"] == "failed"
        await asyncio.sleep(3)
        rows = await _attempts(maker, task_id)
        assert [r["reconciliation"] for r in rows] == ["resultado_incierto"]
        assert int(rows[0]["tool_events"] or 0) >= 1
        assert (rows[0]["attempt_id"], "skill_manage") in await _inicios_registrados(maker, task_id)
        assert len(handler.requests) == 2
        async with maker() as session:
            db_task = await session.get(Task, task_id)
        assert db_task.status == "failed"
        assert db_task.metadata_json["recovery"]["estado"] == "resultado_incierto"
        assert "recovery_reintentos" not in db_task.metadata_json


class TestStreamCortadoEnUnaHerramienta:
    async def test_stream_cortado_sin_resultado_no_se_da_por_completado(self, stack_herramientas):
        maker = stack_herramientas["maker"]
        a = stack_herramientas["spawn"]("instancia-corte-stream", extra_env={"MODEL_CALL_TIMEOUT_SECONDS": "30"})
        await a.wait_ready()
        headers = await _login(a.base)
        task_id = (
            await _api(
                "POST",
                a.base,
                "/tasks",
                headers,
                json={"agent_id": stack_herramientas["agent_id"], "prompt": "Sincroniza el correo CORTE_STREAM"},
            )
        )["id"]
        final = await _until(lambda: _final_status(a.base, headers, task_id), 400, "fin de la tarea")
        async with maker() as session:
            db_task = await session.get(Task, task_id)
        assert final["status"] == "failed", (final["status"], db_task.response)
        assert "Stream stalled" not in (db_task.response or "")
        assert "Stream stalled" not in (db_task.error_message or "")


PATRON_BUCLE_VISION = re.compile(r"BUCLE_VISION=(\S+)")


class _StubBucleVision(_StubConHerramientas):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        mensajes = request.get("messages") or []
        texto = " ".join(str(m.get("content") or "") for m in mensajes if m.get("role") == "user")
        imagen = PATRON_BUCLE_VISION.search(texto)
        type(self).requests.append(request)
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        turno = sum(1 for m in mensajes if m.get("role") == "tool")
        if imagen and request.get("tools"):
            argumentos = json.dumps({"image_url": imagen.group(1), "question": f"¿Qué dice la fila {turno + 1}?"})
            chunks = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_vision_{turno}",
                                        "type": "function",
                                        "function": {"name": "vision_analyze", "arguments": argumentos},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
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
                            "delta": {
                                "role": "assistant",
                                "content": (
                                    "Cambio descrito con lo que ya vi."
                                    if imagen
                                    else "La imagen muestra una tabla de tres columnas."
                                ),
                            },
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


class TestBucleSobreLaMismaImagen:
    async def test_preguntas_distintas_sobre_una_imagen_activan_el_cortacircuito(self, stack_herramientas):
        maker, agent_id = stack_herramientas["maker"], stack_herramientas["agent_id"]
        stack_herramientas["handler"].do_POST = _StubBucleVision.do_POST
        async with maker() as session:
            agente = await session.get(Agent, agent_id)
            agente.max_iterations = 14
            agente.enabled_toolsets = ["file", "vision"]
            await session.commit()
        imagen = stack_herramientas["workspace"] / "uploads" / "pegada.png"
        imagen.parent.mkdir(exist_ok=True)
        imagen.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        hermes_home = stack_herramientas["workspace"] / ".hermes"
        hermes_home.mkdir(exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "auxiliary:\n  vision:\n    provider: custom\n"
            f"    base_url: {stack_herramientas['llm_url']}\n    api_key: clave-simulada\n    model: modelo-simulado\n",
            encoding="utf-8",
        )
        a = stack_herramientas["spawn"]("instancia-bucle-vision")
        await a.wait_ready()
        headers = await _login(a.base)
        task_id = (
            await _api(
                "POST",
                a.base,
                "/tasks",
                headers,
                json={"agent_id": agent_id, "prompt": f"quiero hacer otro cambio BUCLE_VISION={imagen}"},
            )
        )["id"]
        final = await _until(lambda: _final_status(a.base, headers, task_id), 300, "fin de la tarea")
        principales = [r for r in stack_herramientas["handler"].requests if PATRON_BUCLE_VISION.search(json.dumps(r))]
        con_herramientas = [r for r in principales if r.get("tools")]
        async with maker() as session:
            tarea = await session.get(Task, task_id)
        llamadas = [c for c in (tarea.tool_calls or []) if c.get("name") == "vision_analyze"]
        assert len(con_herramientas) <= 5, (len(con_herramientas), len(principales))
        assert len(llamadas) <= 4, llamadas
        nombres = sorted({t["function"]["name"] for r in con_herramientas for t in r["tools"]})
        assert final["status"] == "completed", (final["status"], tarea.error_message, nombres)
        assert "Cambio descrito" in (tarea.response or "") or (tarea.response or "").strip()
        tiempos = (tarea.metadata_json or {}).get("tiempos") or {}
        assert (tiempos.get("racha_maxima") or {}).get("herramienta") == "vision_analyze", tiempos
        llamadas = [c for c in tiempos.get("ultimas_llamadas") or [] if c["name"] == "vision_analyze"]
        assert len(llamadas) >= 3 and all("pegada.png" in c["args"] for c in llamadas), tiempos.get("ultimas_llamadas")
        assert len({c["huella"] for c in llamadas}) == len(llamadas), "preguntas distintas: huellas distintas"


async def _fines_registrados(maker, task_id: str) -> list[tuple[str, str, str]]:
    async with maker() as session:
        filas = await session.execute(
            text(
                "SELECT details->>'attempt_id', message, details->>'clase' FROM activity_logs "
                "WHERE task_id = :t AND event_type = 'task.tool_finished' ORDER BY created_at"
            ),
            {"t": task_id},
        )
        return [(str(a), str(m), str(c)) for a, m, c in filas.all()]


class TestHerramientaTerminada:
    async def test_cada_herramienta_deja_su_fin_clasificado_con_el_intento(self, stack_herramientas):
        maker = stack_herramientas["maker"]
        archivo = stack_herramientas["workspace"] / "datos.txt"
        archivo.write_text("contenido-clasificado", encoding="utf-8")
        inst = stack_herramientas["spawn"]("instancia-fin-herramienta")
        await inst.wait_ready()
        headers = await _login(inst.base)
        marca = f"marca-{uuid.uuid4().hex[:8]}"
        task_id = (
            await _api(
                "POST",
                inst.base,
                "/tasks",
                headers,
                json={"agent_id": stack_herramientas["agent_id"], "prompt": f"LEER={archivo} {marca}"},
            )
        )["id"]
        final = await _until(lambda: _final_status(inst.base, headers, task_id), 120, "tarea")
        assert final["status"] == "completed"
        inicios = await _inicios_registrados(maker, task_id)
        fines = await _fines_registrados(maker, task_id)
        assert fines, "el runner debía registrar el fin de la herramienta"
        assert [(a, m) for a, m, _ in fines] == inicios
        assert {c for _, _, c in fines} == {"ok"}

    async def test_una_lectura_que_falla_no_se_registra_como_ok(self, stack_herramientas):
        maker = stack_herramientas["maker"]
        inst = stack_herramientas["spawn"]("instancia-fin-herramienta-falla")
        await inst.wait_ready()
        headers = await _login(inst.base)
        marca = f"marca-{uuid.uuid4().hex[:8]}"
        faltante = stack_herramientas["workspace"] / "no-existe.txt"
        task_id = (
            await _api(
                "POST",
                inst.base,
                "/tasks",
                headers,
                json={"agent_id": stack_herramientas["agent_id"], "prompt": f"LEER={faltante} {marca}"},
            )
        )["id"]
        final = await _until(lambda: _final_status(inst.base, headers, task_id), 120, "tarea")
        assert final["status"] == "completed"
        fines = await _fines_registrados(maker, task_id)
        assert fines and all(c != "ok" for _, _, c in fines), fines
