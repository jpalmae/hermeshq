from __future__ import annotations

import io
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import _api, _final_status, _Instance, _login, _scratch_url, _until
from test_execution_lease import _FakeLLM

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task

FILA = "| CRÍTICO | RE: Sesión de avance - Proyecto de prueba | Persona Ficticia | 2026-11-11 | PENDIENTE_DE_MÍ |\n"


def _tabla_variada(filas: int) -> str:
    return "".join(
        f"| {n} | Asunto distinto número {n * 7919 % 10007} | Remitente {n % 37} | 2026-11-{n % 28 + 1:02d} | "
        f"{'ALTA' if n % 3 else 'MEDIA'} |\n"
        for n in range(filas)
    )


class _Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _AgenteInterrumpible:
    def __init__(self):
        self.interrupciones = 0

    def interrupt(self, message=None):
        self.interrupciones += 1


def _guardia(**config):
    reloj = _Reloj()
    guardia = runner.GuardiaDesbocada({"activa": True, **config}, ahora=reloj)
    agente = _AgenteInterrumpible()
    guardia.vincular(agente)
    guardia.paso(5)
    return guardia, agente, reloj


class TestGuardiaDesbocada:
    def test_fila_repetida_se_corta_una_sola_vez(self):
        guardia, agente, _ = _guardia()
        for _ in range(400):
            guardia.texto(FILA)
        assert guardia.disparo["motivo"] == "repeticion"
        assert guardia.disparo["paso"] == 5
        assert guardia.disparo["caracteres"] < 12000
        assert agente.interrupciones == 1

    def test_tabla_larga_real_no_se_corta(self):
        guardia, agente, _ = _guardia()
        guardia.texto(_tabla_variada(400))
        for linea in _tabla_variada(400).splitlines(keepends=True):
            guardia.texto(linea)
        assert guardia.disparo is None
        assert agente.interrupciones == 0

    def test_respuesta_mas_larga_vista_en_produccion_no_se_corta(self):
        guardia, agente, _ = _guardia()
        texto = _tabla_variada(450)
        assert len(texto) > 30860
        for i in range(0, len(texto), 7):
            guardia.texto(texto[i : i + 7])
        assert guardia.disparo is None

    def test_texto_sin_repeticion_por_encima_del_tope_se_corta_por_largo(self):
        guardia, agente, _ = _guardia(max_caracteres=20000)
        guardia.texto(_tabla_variada(400))
        assert guardia.disparo["motivo"] == "largo"
        assert agente.interrupciones == 1

    def test_paso_del_modelo_demasiado_largo_se_corta_por_tiempo(self):
        guardia, agente, reloj = _guardia(max_segundos=900)
        reloj.t += 899
        guardia.revisar_tiempo()
        assert guardia.disparo is None
        reloj.t += 2
        guardia.revisar_tiempo()
        assert guardia.disparo["motivo"] == "tiempo"
        assert guardia.disparo["segundos"] == 901
        assert agente.interrupciones == 1

    def test_el_tiempo_de_las_herramientas_no_cuenta(self):
        guardia, agente, reloj = _guardia(max_segundos=900)
        guardia.herramientas()
        reloj.t += 5000
        guardia.revisar_tiempo()
        assert guardia.disparo is None
        guardia.paso(6)
        reloj.t += 10
        guardia.revisar_tiempo()
        assert guardia.disparo is None

    def test_cada_paso_empieza_de_cero(self):
        guardia, _, _ = _guardia(max_caracteres=20000)
        guardia.texto(_tabla_variada(150))
        guardia.paso(6)
        guardia.texto(_tabla_variada(150))
        assert guardia.disparo is None

    def test_desactivada_no_interrumpe(self):
        guardia, agente, reloj = _guardia(activa=False)
        for _ in range(2000):
            guardia.texto(FILA)
        reloj.t += 10_000
        guardia.revisar_tiempo()
        assert guardia.disparo is None
        assert agente.interrupciones == 0

    def test_aviso_y_mensajes_sin_el_texto_desbocado(self):
        guardia, _, _ = _guardia()
        for _ in range(400):
            guardia.texto(FILA)
        aviso = guardia.aviso()
        assert "texto repetido" in aviso and "Lo que ya se ejecutó con herramientas" in aviso
        mensajes = [
            {"role": "user", "content": "resumen"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            {"role": "assistant", "content": FILA * 50},
            {"role": "assistant", "content": FILA * 50},
        ]
        limpios = runner.mensajes_sin_desborde(mensajes, aviso)
        assert limpios[:3] == mensajes[:3]
        assert limpios[3:] == [{"role": "assistant", "content": aviso}]


class _AgenteQueSeDesboca:
    def __init__(self, kwargs, filas):
        self.callback = kwargs["stream_delta_callback"]
        self.paso = kwargs["step_callback"]
        self.progreso = kwargs["tool_progress_callback"]
        self.filas = filas
        self.interrumpido = False
        self.emitidas = 0

    def interrupt(self, message=None):
        self.interrumpido = True

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.paso(1)
        self.progreso("tool.started", "ms365_correo_sync")
        self.progreso("tool.completed", "ms365_correo_sync", duration=0.5, is_error=False)
        self.paso(2)
        texto = []
        for fila in self.filas:
            if self.interrumpido:
                break
            self.callback(fila)
            texto.append(fila)
            self.emitidas += 1
        mensajes = [
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "ms365_correo_sync"}}],
            },
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            {"role": "assistant", "content": "".join(texto)},
        ]
        return {"final_response": "".join(texto), "messages": mensajes, "api_calls": 2, "interrupted": True}


def _correr_main(monkeypatch, tmp_path, filas, guardia=None):
    eventos = []
    creados = []

    def fabrica(**kwargs):
        creados.append(_AgenteQueSeDesboca(kwargs, filas))
        return creados[-1]

    payload = {
        "task_id": "t-desbocada",
        "prompt": "resume mis correos no leídos de la semana",
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        **({"guardia_desbocada": guardia} if guardia is not None else {}),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    monkeypatch.setattr(runner, "_abrir_salida", lambda: None)
    monkeypatch.setattr(runner, "_proteger_proceso", lambda: None)
    monkeypatch.setattr(runner, "_ocultar_token_a_los_comandos", lambda: None)
    monkeypatch.setattr(runner, "_activar_guardia", lambda p: None)
    monkeypatch.setattr(runner, "_aplicar_overrides_resumen", lambda *a: None)
    monkeypatch.setattr(runner, "_preparar_exposicion", lambda p: None)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=fabrica))
    for clave in ("HERMES_HOME", "HERMES_QUIET", "HERMESHQ_TASK_PAYLOAD", "TERM"):
        monkeypatch.setenv(clave, "")
    monkeypatch.chdir(tmp_path)
    codigo = runner.main()
    resultado = [e for e in eventos if e.get("event") == "result"]
    return codigo, (resultado[0] if resultado else None), creados[0], eventos


class TestRunnerConDesborde:
    def test_bucle_de_filas_se_corta_y_entrega_el_aviso(self, monkeypatch, tmp_path):
        codigo, res, agente, eventos = _correr_main(monkeypatch, tmp_path, [FILA] * 100_000)
        assert codigo == 0
        assert agente.interrumpido and agente.emitidas < 200
        assert res["generacion_desbocada"]["motivo"] == "repeticion"
        assert res["generacion_desbocada"]["paso"] == 2
        assert res["final_response"].startswith("⚠️ Detuve esta respuesta")
        assert FILA not in res["final_response"]
        assert res["messages"][-1] == {"role": "assistant", "content": res["final_response"]}
        assert all(FILA not in str(m.get("content")) for m in res["messages"])
        assert [c["name"] for c in res["tool_calls"]] == ["ms365_correo_sync"]
        assert "accion_narrada" not in res
        deltas = [e["data"] for e in eventos if e.get("event") == "delta"]
        assert deltas[-1] == res["final_response"]
        assert len(deltas) < 200

    def test_respuesta_normal_no_se_toca(self, monkeypatch, tmp_path):
        filas = _tabla_variada(300).splitlines(keepends=True)
        _, res, agente, _ = _correr_main(monkeypatch, tmp_path, filas)
        assert not agente.interrumpido
        assert "generacion_desbocada" not in res
        assert res["final_response"] == "".join(filas).strip()

    def test_guardia_desactivada_deja_pasar_todo(self, monkeypatch, tmp_path):
        _, res, agente, _ = _correr_main(monkeypatch, tmp_path, [FILA] * 3000, guardia={"activa": False})
        assert not agente.interrumpido and agente.emitidas == 3000
        assert "generacion_desbocada" not in res


class _StubDesbocado(_FakeLLM):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(200_000):
                chunk = {**base, "choices": [{"index": 0, "delta": {"content": FILA}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                type(self).emitidas += 1
            fin = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(fin)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            type(self).cortado = True


@pytest.fixture
async def stack_desbocado(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-desbocado"
    workspace.mkdir(parents=True)
    handler = type("LLMDesbocado", (_StubDesbocado,), {"delay": 0.0, "requests": [], "emitidas": 0, "cortado": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-desbocado", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-desbocado",
            slug="agente-desbocado",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file"],
            max_iterations=4,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instance = _Instance("instancia-desbocado", _scratch_url(db_name), workspace, llm_url, tmp_path)
    try:
        await instance.wait_ready()
        yield {"maker": maker, "handler": handler, "agent_id": agent_id, "base": instance.base}
    finally:
        instance.kill_group()
        server.shutdown()


@pytest.mark.integration
@requires_database
class TestGeneracionDesbocadaE2E:
    async def test_el_runner_real_corta_el_bucle_y_la_tarea_termina_con_el_aviso(self, stack_desbocado):
        headers = await _login(stack_desbocado["base"])
        task_id = (
            await _api(
                "POST",
                stack_desbocado["base"],
                "/tasks",
                headers,
                json={"agent_id": stack_desbocado["agent_id"], "prompt": "resume mis correos no leídos"},
            )
        )["id"]
        final = await _until(lambda: _final_status(stack_desbocado["base"], headers, task_id), 120, "fin de la tarea")
        async with stack_desbocado["maker"]() as session:
            tarea = await session.get(Task, task_id)
        assert final["status"] == "completed", (final, tarea.error_message)
        assert tarea.response.startswith("⚠️ Detuve esta respuesta"), tarea.response[:300]
        assert FILA not in tarea.response
        assert tarea.metadata_json["generacion_desbocada"]["motivo"] == "repeticion"
        assert stack_desbocado["handler"].emitidas < 50_000
        assert all(FILA not in str(m.get("content")) for m in (tarea.messages_json or []))


class TestRazonamientoOculto:
    def test_razonamiento_sin_texto_visible_por_encima_del_tope_se_corta(self):
        guardia, agente, _ = _guardia(max_caracteres=20000)
        for _ in range(30):
            guardia.razonamiento("x" * 1000)
        assert guardia.disparo["motivo"] == "razonamiento"
        assert guardia.disparo["paso"] == 5
        assert agente.interrupciones == 1

    def test_razonamiento_dentro_del_tope_no_se_corta(self):
        guardia, agente, _ = _guardia(max_caracteres=20000)
        for _ in range(15):
            guardia.razonamiento("x" * 1000)
        assert guardia.disparo is None
        assert agente.interrupciones == 0

    def test_el_razonamiento_se_cuenta_por_paso(self):
        guardia, agente, _ = _guardia(max_caracteres=20000)
        for _ in range(15):
            guardia.razonamiento("x" * 1000)
        guardia.paso(6)
        for _ in range(15):
            guardia.razonamiento("x" * 1000)
        assert guardia.disparo is None

    def test_razonamiento_y_texto_visible_no_se_suman(self):
        guardia, agente, _ = _guardia(max_caracteres=20000)
        for _ in range(15):
            guardia.razonamiento("x" * 1000)
        guardia.texto(_tabla_variada(200))
        assert guardia.disparo is None

    def test_guardia_inactiva_no_corta_el_razonamiento(self):
        guardia, agente, _ = _guardia(activa=False, max_caracteres=1000)
        guardia.razonamiento("x" * 50000)
        assert guardia.disparo is None
        assert agente.interrupciones == 0

    def test_el_aviso_al_usuario_nombra_el_razonamiento(self):
        guardia, _, _ = _guardia(max_caracteres=1000)
        guardia.razonamiento("x" * 2000)
        assert "razonamiento interno" in guardia.aviso()
