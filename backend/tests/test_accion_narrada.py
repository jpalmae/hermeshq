from __future__ import annotations

import json
import re
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

PEDIDO_REAL = (
    'en esa presentación cambia el título "Principales riesgos" por "Riesgos del trimestre" y agrega un cuarto riesgo'
)
RESPUESTA_REAL = (
    "Voy a editar la presentación. Primero reviso el contenido… Editada. Creo la nueva versión… Listo. "
    "Nueva versión creada (V2): Título cambiado… Se agregó el cuarto riesgo… La versión anterior se conserva tal cual."
)


class TestDetectorPositivos:
    def test_caso_real_de_produccion(self):
        assert runner.accion_narrada(PEDIDO_REAL, RESPUESTA_REAL) == "edicion"

    def test_solo_anuncia_y_termina(self):
        pedido = 'sube el Excel de ventas que hiciste a mi OneDrive, en una carpeta "Pruebas HermesHQ"'
        respuesta = "Necesito localizar primero el Excel de ventas que se generó en esta conversación y luego subirlo a OneDrive."
        assert runner.accion_narrada(pedido, respuesta) == "subida"

    def test_dice_que_no_lo_encuentra_sin_haber_buscado(self):
        pedido = (
            "compárteme un enlace de solo lectura para mi organización del archivo Analisis_Migracion_Correo_Nube.pdf"
        )
        respuesta = (
            "No encuentro el archivo `Analisis_Migracion_Correo_Nube.pdf` en tu OneDrive o SharePoint, así que no puedo "
            "generar el enlace de lectura todavía. ¿Cómo lo localizo?"
        )
        assert runner.accion_narrada(pedido, respuesta) == "subida"

    def test_anunciar_y_preguntar_no_es_intencion_vacia(self):
        assert not runner.solo_intencion("Voy a subirlo a OneDrive. ¿En qué carpeta lo quieres?")
        assert not runner.solo_intencion("No encontré el Excel en tu OneDrive; voy a necesitar que me lo envíes.")

    @pytest.mark.parametrize(
        ("pedido", "respuesta", "tipo"),
        [
            (
                "responde el correo de Juan diciendo que mañana voy",
                "✅ Respondí el correo de Juan: le dije que mañana vas.",
                "correo",
            ),
            ("reenvía ese correo a maria@empresa.cl", "Listo, reenviado a maria@empresa.cl.", "correo"),
            ("envíale un mail a Pedro con el resumen", "Correo enviado a Pedro con el resumen.", "correo"),
            (
                "programa un recordatorio todos los lunes a las 9 para revisar ventas",
                "Programado. Te recordaré cada lunes a las 9:00.",
                "programacion",
            ),
            (
                "crea un cron que revise mi bandeja cada hora",
                "Listo, tarea creada: revisaré tu bandeja cada hora.",
                "programacion",
            ),
            (
                "agenda una reunión con Pedro mañana a las 10",
                "Reunión agendada con Pedro mañana 10:00. ¿Necesitas algo más?",
                "programacion",
            ),
            (
                "sube el informe a la carpeta de SharePoint de Finanzas",
                "Subido a SharePoint, carpeta Finanzas.",
                "subida",
            ),
            ("comparte el archivo en OneDrive con Ana", "Hecho: lo compartí con Ana en OneDrive.", "subida"),
            ("genera un excel con las ventas", "He creado el archivo ventas.xlsx.\nMEDIA:/ws/ventas.xlsx", "creacion"),
            ("arma una presentación de 5 láminas sobre el proyecto", "MEDIA:/ws/proyecto.pptx", "creacion"),
            ("agrégale una conclusión al documento", "Documento actualizado con la conclusión. ¿Algo más?", "edicion"),
            ("elimina la lámina 3 de la presentación", "Lámina 3 eliminada ✅", "edicion"),
        ],
    )
    def test_variantes(self, pedido, respuesta, tipo):
        assert runner.accion_narrada(pedido, respuesta) == tipo


class TestNoEncontradoSinBuscar:
    def test_caso_real_busca_y_mueve_la_reunion(self):
        pedido = "Búscala en mi calendario de mañana y muévela a las 11:00 a 11:30."
        respuesta = 'No encontré la reunión "Batería K ciclo 1" en el calendario de mañana, así que no puedo moverla.'
        assert runner.accion_narrada(pedido, respuesta) == "busqueda"

    def test_caso_real_no_pude_encontrar_y_la_herramienta_no_devuelve(self):
        pedido = 'Lista mis eventos de mañana con la herramienta de calendario y mueve "Batería K ciclo 1" a las 11:00.'
        for respuesta in (
            'No pude encontrar el evento "Batería K ciclo 1" en tu calendario de mañana.',
            "La herramienta de calendario no devuelve ese evento, así que no puedo moverlo.",
        ):
            assert runner.accion_narrada(pedido, respuesta) is not None, respuesta

    @pytest.mark.parametrize(
        "pedido", ["abre el correo de Ana", "revisa si llegó la factura", "muéstrame el Excel de ventas"]
    )
    def test_variantes(self, pedido):
        assert runner.accion_narrada(pedido, "No encuentro ese archivo en tu workspace.") == "busqueda"

    @pytest.mark.parametrize(
        ("pedido", "respuesta"),
        [
            ("¿qué es un cron?", "No existe un cron en tu cuenta todavía; es una tarea programada."),
            ("busca la factura de Ana", "Encontré 2 facturas de Ana. ¿Cuál quieres?"),
            ("hola", "No encuentro nada pendiente."),
        ],
    )
    def test_negativos(self, pedido, respuesta):
        assert runner.accion_narrada(pedido, respuesta) is None


class TestDetectorNegativos:
    @pytest.mark.parametrize(
        ("pedido", "respuesta"),
        [
            (PEDIDO_REAL, "¿Quieres que cambie solo el título de la lámina 2 o también el del índice?"),
            (PEDIDO_REAL, "La presentación tiene 3 riesgos. ¿Cuál es el cuarto riesgo que quieres agregar?"),
            (PEDIDO_REAL, "Te propongo este cuarto riesgo: ciberataques. ¿Lo agrego?"),
            (PEDIDO_REAL, "No pude editar la presentación: no tengo permisos de escritura sobre ese archivo."),
            (PEDIDO_REAL, "Todavía no se ha creado la nueva versión; necesito el archivo original."),
            ("responde el correo de Juan", "Este es el borrador:\n\nHola Juan, mañana voy.\n\n¿Lo envío?"),
            ("¿qué es un cron?", "Un cron es una tarea programada que se ejecuta periódicamente."),
            ("¿cómo cambio el título de una presentación?", "Abre la lámina y edita el cuadro del título."),
            ("resume esta presentación", "La presentación fue creada en 2024 y tiene 5 láminas."),
            ("cambia el tono de este párrafo: hola", "Cambiado: «Estimado, buenos días»."),
            ("hola, ¿cómo estás?", "¡Listo para ayudarte! ¿Qué necesitas?"),
            ("el recordatorio de ayer no llegó", "Estaba programado para las 9, pero el servidor de correo falló."),
        ],
    )
    def test_no_es_accion_narrada(self, pedido, respuesta):
        assert runner.accion_narrada(pedido, respuesta) is None


class TestTextosDeterministas:
    def test_nota_y_aviso(self):
        nota = runner.nota_accion_narrada("edicion")
        assert "no llamaste ninguna herramienta" in nota and "di claramente que no pudiste" in nota
        assert runner.aviso_accion_no_hecha("edicion") == (
            "No pude completar el cambio: no se ejecutó ninguna herramienta, así que no cambió nada. Vuelve a pedírmelo."
        )

    def test_llamadas_solo_del_turno_actual(self):
        mensajes = [
            {"role": "user", "content": "antes"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]},
            {"role": "tool", "content": "{}"},
            {"role": "user", "content": PEDIDO_REAL},
            {"role": "assistant", "content": RESPUESTA_REAL},
        ]
        assert runner._llamadas_desde(mensajes, runner._indice_ultimo_usuario(mensajes)) == 0
        assert runner._llamadas_desde(mensajes, 0) == 1


class _AgenteFalso:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.llamadas.append(user_message)
        texto, herramienta = self.respuestas.pop(0)
        mensajes = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        if herramienta:
            mensajes += [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": herramienta}}]},
                {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 2 if herramienta else 1}


def _correr_main(monkeypatch, tmp_path, respuestas, guardia=True):
    agente = _AgenteFalso(respuestas)
    eventos = []
    payload = {
        "task_id": "t-narrada",
        "prompt": PEDIDO_REAL,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "guardia_accion_narrada": guardia,
    }
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    monkeypatch.setattr(runner, "_abrir_salida", lambda: None)
    monkeypatch.setattr(runner, "_proteger_proceso", lambda: None)
    monkeypatch.setattr(runner, "_ocultar_token_a_los_comandos", lambda: None)
    monkeypatch.setattr(runner, "_activar_guardia", lambda p: None)
    monkeypatch.setattr(runner, "_aplicar_overrides_resumen", lambda *a: None)
    monkeypatch.setattr(runner, "_preparar_exposicion", lambda p: None)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=lambda **kw: agente))
    for clave in ("HERMES_HOME", "HERMES_QUIET", "HERMESHQ_TASK_PAYLOAD", "TERM"):
        monkeypatch.setenv(clave, "")
    monkeypatch.chdir(tmp_path)
    codigo = runner.main()
    resultado = [e for e in eventos if e.get("event") == "result"]
    return codigo, (resultado[0] if resultado else None), agente


class TestRunnerConAgenteFalso:
    def test_reintento_que_llama_la_herramienta_entrega_el_resultado_real(self, monkeypatch, tmp_path):
        codigo, res, agente = _correr_main(
            monkeypatch, tmp_path, [(RESPUESTA_REAL, None), ("Listo, guardé la V2.", "write_file")]
        )
        assert codigo == 0
        assert res["final_response"] == "Listo, guardé la V2."
        assert res["accion_narrada"]["resultado"] == "corregida"
        assert len(agente.llamadas) == 2 and "no llamaste ninguna herramienta" in agente.llamadas[1]
        assert [c["name"] for c in res["tool_calls"]] == ["write_file"]
        assert res["iterations"] == 3

    def test_reintento_que_vuelve_a_narrar_entrega_el_aviso_honesto(self, monkeypatch, tmp_path):
        _, res, agente = _correr_main(monkeypatch, tmp_path, [(RESPUESTA_REAL, None), (RESPUESTA_REAL, None)])
        assert res["final_response"] == runner.aviso_accion_no_hecha("edicion")
        assert res["accion_narrada"] == {
            "detectada": "edicion",
            "reintento": True,
            "respuesta_descartada": RESPUESTA_REAL[:500],
            "resultado": "aviso_honesto",
        }
        assert len(agente.llamadas) == 2

    def test_reintento_que_admite_no_poder_se_entrega_tal_cual(self, monkeypatch, tmp_path):
        honesto = "No pude editar la presentación: no tengo acceso al archivo."
        _, res, _ = _correr_main(monkeypatch, tmp_path, [(RESPUESTA_REAL, None), (honesto, None)])
        assert res["final_response"] == honesto
        assert res["accion_narrada"]["resultado"] == "sin_afirmacion"

    def test_con_herramientas_en_el_turno_no_se_reintenta(self, monkeypatch, tmp_path):
        _, res, agente = _correr_main(monkeypatch, tmp_path, [(RESPUESTA_REAL, "write_file")])
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res

    def test_guardia_desactivada_no_reintenta(self, monkeypatch, tmp_path):
        _, res, agente = _correr_main(monkeypatch, tmp_path, [(RESPUESTA_REAL, None)], guardia=False)
        assert len(agente.llamadas) == 1
        assert res["final_response"] == RESPUESTA_REAL


PATRON_ESCRIBIR = re.compile(r"ESCRIBIR=(\S+)")
NOTA = "Tu respuesta anterior afirmó haber hecho"


class _StubNarrador(_FakeLLM):
    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        mensajes = request.get("messages") or []
        usuario = " ".join(str(m.get("content") or "") for m in mensajes if m.get("role") == "user")
        hay_resultado = any(m.get("role") == "tool" for m in mensajes)
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        ruta = PATRON_ESCRIBIR.search(usuario)
        if NOTA in usuario and "MODO=corrige" in usuario and not hay_resultado and ruta:
            argumentos = json.dumps({"path": ruta.group(1), "content": "Riesgos del trimestre\n4. Riesgo nuevo"})
            llamada = {
                "index": 0,
                "id": "call_escribir_1",
                "type": "function",
                "function": {"name": "write_file", "arguments": argumentos},
            }
            self._sse(
                [
                    {
                        **base,
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant", "tool_calls": [llamada]}, "finish_reason": None}
                        ],
                    },
                    {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
            )
            return
        texto = "Listo: guardé la versión 2 con el título nuevo." if hay_resultado else RESPUESTA_REAL
        self._sse(
            [
                {
                    **base,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": texto}, "finish_reason": None}],
                },
                {
                    **base,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
        )


@pytest.fixture
async def stack_narrador(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-narrador"
    workspace.mkdir(parents=True)
    handler = type("LLMNarrador", (_StubNarrador,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-narrador", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-narrador",
            slug="agente-narrador",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=["file"],
            max_iterations=4,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instance = _Instance("instancia-narrador", _scratch_url(db_name), workspace, llm_url, tmp_path)
    try:
        await instance.wait_ready()
        yield {"maker": maker, "handler": handler, "agent_id": agent_id, "base": instance.base, "workspace": workspace}
    finally:
        instance.kill_group()
        server.shutdown()


async def _ejecutar(stack, modo: str):
    destino = stack["workspace"] / "presentacion_v2.txt"
    headers = await _login(stack["base"])
    prompt = f"{PEDIDO_REAL} ESCRIBIR={destino} MODO={modo}"
    task_id = (
        await _api("POST", stack["base"], "/tasks", headers, json={"agent_id": stack["agent_id"], "prompt": prompt})
    )["id"]
    final = await _until(lambda: _final_status(stack["base"], headers, task_id), 120, "fin de la tarea")
    async with stack["maker"]() as session:
        tarea = await session.get(Task, task_id)
    return final, tarea, destino


@pytest.mark.integration
@requires_database
class TestAccionNarradaE2E:
    async def test_primer_turno_narra_y_el_reintento_hace_el_cambio(self, stack_narrador):
        final, tarea, destino = await _ejecutar(stack_narrador, "corrige")
        assert final["status"] == "completed"
        assert destino.exists(), tarea.response
        assert "Riesgos del trimestre" in destino.read_text(encoding="utf-8")
        assert tarea.response == "Listo: guardé la versión 2 con el título nuevo."
        assert tarea.metadata_json["accion_narrada"]["resultado"] == "corregida"
        assert [c["name"] for c in tarea.tool_calls] == ["write_file"]

    async def test_ambos_turnos_narran_y_se_entrega_el_aviso_honesto(self, stack_narrador):
        final, tarea, destino = await _ejecutar(stack_narrador, "insiste")
        assert final["status"] == "completed"
        assert not destino.exists()
        assert tarea.response == runner.aviso_accion_no_hecha("edicion")
        assert "Nueva versión creada" not in (tarea.response or "")
        assert tarea.metadata_json["accion_narrada"]["resultado"] == "aviso_honesto"
        assert sum(1 for r in stack_narrador["handler"].requests if NOTA in json.dumps(r, ensure_ascii=False)) == 1


class TestSkillEliminadaSinHerramienta:
    def test_caso_real(self):
        pedido = "Elimina la skill «firma-de-prueba-qa», ya terminé la prueba."
        assert runner.accion_narrada(pedido, "Hecho. La skill «firma-de-prueba-qa» queda eliminada.") == "edicion"

    def test_preguntar_por_una_skill_no_es_una_accion(self):
        assert runner.accion_narrada("¿qué hace la skill de correos?", "Resume los correos no leídos.") is None
