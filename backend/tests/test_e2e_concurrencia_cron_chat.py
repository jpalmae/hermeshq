import asyncio
import hashlib
import hmac
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_http_recuperacion import _api, _Instance, _login, _scratch_url
from test_e2e_reset_aislamiento import _clave_publica, _sesion
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

JWT_SECRET_INSTANCIA = "secreto-jwt-pruebas-e2e-" + "x" * 16
MEDICIONES: list[dict] = []


def _ultimo_usuario(request: dict) -> str:
    usuario = [m for m in (request.get("messages") or []) if m.get("role") == "user"]
    contenido = usuario[-1].get("content") if usuario else ""
    return contenido if isinstance(contenido, str) else json.dumps(contenido, ensure_ascii=False)


class _LLMTemporizado(_FakeLLM):
    registros: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        ultimo = _ultimo_usuario(request)
        demora = re.search(r"DEMORA=(\d+(?:\.\d+)?)", ultimo)
        registro = {"marcas": re.findall(r"MARCA-\w+", ultimo), "inicio": time.time(), "fin": None}
        type(self).registros.append(registro)
        if demora:
            time.sleep(float(demora.group(1)))
        respuesta = f"ECO: {' '.join(registro['marcas']) or ultimo[-40:]}"
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
        chunks = [
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": respuesta}, "finish_reason": None}],
            },
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        registro["fin"] = time.time()


@pytest.fixture
async def pila(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    raiz = tmp_path / "workspaces"
    comun = raiz / "comun"
    comun.mkdir(parents=True)
    handler = type("LLMConcurrencia", (_LLMTemporizado,), {"delay": 0.0, "requests": [], "registros": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    agentes: dict[str, str] = {}
    async with maker() as session:
        node = Node(name="nodo-concurrencia", hostname="localhost")
        session.add(node)
        await session.flush()
        for letra in "abcdef":
            espacio = raiz / f"agente-{letra}"
            espacio.mkdir()
            agente = Agent(
                node_id=node.id,
                name=f"agente-{letra}",
                slug=f"agente-conc-{letra}",
                workspace_path=str(espacio),
                status="running",
                model="modelo-simulado",
                enabled_toolsets=[],
                max_iterations=2,
            )
            session.add(agente)
            await session.flush()
            agentes[letra] = agente.id
        await session.commit()
    instancias: list[_Instance] = []
    pendientes: list[asyncio.Task] = []

    def spawn(nombre: str, extra_env: dict | None = None) -> _Instance:
        inst = _Instance(
            nombre,
            _scratch_url(db_name),
            comun,
            llm_url,
            tmp_path,
            extra_env={"HQ_E2E_WORKSPACE_POR_AGENTE": "1", **(extra_env or {})},
        )
        instancias.append(inst)
        return inst

    try:
        yield {"maker": maker, "handler": handler, "agentes": agentes, "spawn": spawn, "pendientes": pendientes}
    finally:
        for pendiente in pendientes:
            pendiente.cancel()
        await asyncio.gather(*pendientes, return_exceptions=True)
        for inst in instancias:
            inst.kill_group()
        server.shutdown()


async def _esperar(condicion, timeout: float, descripcion: str):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        valor = await condicion()
        if valor:
            return valor
        await asyncio.sleep(0.25)
    raise AssertionError(f"no ocurrió a tiempo: {descripcion}")


def _llamadas(handler, marca: str) -> list[dict]:
    return [r for r in list(handler.registros) if marca in r["marcas"]]


async def _llamada(handler, marca: str):
    return bool(_llamadas(handler, marca))


async def _alguna(handler, *marcas: str):
    return any(_llamadas(handler, m) for m in marcas)


async def _id_admin(maker) -> str:
    async with maker() as s:
        return (await s.execute(text("SELECT id FROM users WHERE username = 'admin'"))).scalar_one()


async def _cron(maker, agent_id: str, nombre: str, prompt: str, *, vencido: bool = True) -> str:
    dueno = await _id_admin(maker)
    proxima = datetime.now(UTC) + (timedelta(seconds=-2) if vencido else timedelta(days=30))
    async with maker() as s:
        fila = ScheduledTask(
            agent_id=agent_id,
            name=nombre,
            cron_expression="0 0 1 1 *",
            prompt=prompt,
            enabled=True,
            next_run=proxima,
            timezone="UTC",
            owner_user_id=dueno,
            approval_status="approved",
        )
        s.add(fila)
        await s.commit()
        return fila.id


async def _tareas_de_cron(maker, schedule_id: str) -> list[Task]:
    async with maker() as s:
        filas = await s.execute(
            select(Task)
            .where(Task.metadata_json["scheduled_task_id"].as_string() == schedule_id)
            .order_by(Task.queued_at.asc())
        )
        return list(filas.scalars().all())


async def _tareas_con(maker, marca: str) -> list[Task]:
    async with maker() as s:
        filas = await s.execute(select(Task).where(Task.prompt.contains(marca)).order_by(Task.queued_at.asc()))
        return list(filas.scalars().all())


async def _tarea(maker, task_id: str) -> Task:
    async with maker() as s:
        return await s.get(Task, task_id)


async def _terminada(maker, task_id: str):
    tarea = await _tarea(maker, task_id)
    return tarea if tarea and tarea.status in ("completed", "failed", "cancelled") else None


def _medir(caso: str, etiqueta: str, tarea: Task, handler, tipo: str | None = None) -> dict:
    marcas = re.findall(r"MARCA-\w+", tarea.prompt)
    cola_inicio = tarea.queued_at.timestamp()
    llamadas = [r for r in list(handler.registros) if set(marcas) & set(r["marcas"]) and r["inicio"] >= cola_inicio - 1]
    fila = {
        "caso": caso,
        "tarea": etiqueta,
        "tipo": tipo or etiqueta,
        "estado": tarea.status,
        "cola_s": round((tarea.started_at.timestamp() - cola_inicio), 2) if tarea.started_at else None,
        "hasta_llm_s": round(llamadas[0]["inicio"] - cola_inicio, 2) if llamadas else None,
        "total_s": round(tarea.completed_at.timestamp() - cola_inicio, 2) if tarea.completed_at else None,
    }
    MEDICIONES.append(fila)
    print("MEDICION", json.dumps(fila, ensure_ascii=False))
    return fila


async def _chat(base: str, sesion: str, token: str, contenido: str, ip: str | None = None) -> dict:
    inicio = time.monotonic()
    cabeceras = {"X-Session-Token": token, **({"X-Real-IP": ip} if ip else {})}
    async with (
        httpx.AsyncClient(timeout=240) as c,
        c.stream(
            "POST",
            f"{base}/api/public/chat/sessions/{sesion}/messages",
            headers=cabeceras,
            json={"content": contenido},
        ) as r,
    ):
        assert r.status_code == 200, await r.aread()
        async for linea in r.aiter_lines():
            if linea.startswith("data: "):
                evento = json.loads(linea[6:])
                if evento.get("type") in ("done", "error", "cancelled"):
                    evento["duracion_cliente_s"] = round(time.monotonic() - inicio, 2)
                    return evento
    raise AssertionError("el chat no terminó")


async def _sesion_teams(pila, inst: _Instance, letra: str, remitente: str) -> tuple[str, str]:
    from hermeshq.models.agent_assignment import AgentAssignment

    maker, agent_id = pila["maker"], pila["agentes"][letra]
    dueno = await _id_admin(maker)
    async with maker() as s:
        existe = await s.execute(
            select(AgentAssignment.id).where(AgentAssignment.user_id == dueno, AgentAssignment.agent_id == agent_id)
        )
        if existe.first() is None:
            s.add(AgentAssignment(user_id=dueno, agent_id=agent_id))
            await s.commit()
    clave = await _clave_publica(maker, agent_id)
    pila.setdefault("claves", {})[remitente] = clave
    return await _sesion(inst.base, clave, remitente)


async def _sondeo_del_bridge(base: str, clave: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{base}/api/public/chat/notifications", headers={"X-Api-Key": clave})
    if r.status_code == 429:
        return []
    assert r.status_code == 200, r.text
    return r.json()


async def _identidad_del_chat(maker, marca: str) -> str | None:
    return ((await _tareas_con(maker, marca))[0].metadata_json or {}).get("thread_user_id")


def _token_agente(agent_id: str) -> str:
    return hmac.new(JWT_SECRET_INSTANCIA.encode(), f"agent:{agent_id}".encode(), hashlib.sha256).hexdigest()


class TestCronYChatMismoAgente:
    async def test_chat_de_teams_inicia_sin_esperar_al_cron_lento_del_mismo_agente(self, pila):
        maker, handler = pila["maker"], pila["handler"]
        inst = pila["spawn"]("conc-cron-lento")
        await inst.wait_ready()
        sesion, token = await _sesion_teams(pila, inst, "a", "aad-usuario-1")
        cron_id = await _cron(
            maker, pila["agentes"]["a"], "resumen-lento", "Resume el correo DEMORA=20 MARCA-CRONLENTO"
        )
        await _esperar(lambda: _llamada(handler, "MARCA-CRONLENTO"), 40, "disparo real del cron por el scheduler")

        respuesta = await _chat(inst.base, sesion, token, "Hola MARCA-CHATUNO DEMORA=1")
        assert respuesta["type"] == "done", respuesta
        assert "MARCA-CHATUNO" in respuesta["content"]
        chat = (await _tareas_con(maker, "MARCA-CHATUNO"))[0]
        assert await _identidad_del_chat(maker, "MARCA-CHATUNO") == await _id_admin(maker)
        cron = (await _tareas_de_cron(maker, cron_id))[0]
        assert cron.status == "running", "el chat terminó mientras el cron seguía en curso"
        medida_chat = _medir("1/5", "chat (cron lento activo)", chat, handler)
        assert medida_chat["cola_s"] < 2.0
        assert respuesta["duracion_cliente_s"] < 10

        cron = await _esperar(lambda: _terminada(maker, cron.id), 60, "fin del cron lento")
        assert cron.status == "completed"
        assert cron.completed_at > chat.completed_at
        medida_cron = _medir("1/5", "cron lento", cron, handler)
        assert medida_cron["total_s"] >= 20
        assert len(_llamadas(handler, "MARCA-CRONLENTO")) == 1
        assert len(_llamadas(handler, "MARCA-CHATUNO")) == 1

    async def test_varios_crons_del_mismo_agente_se_serializan_sin_perderse_ni_duplicarse(self, pila):
        maker, handler = pila["maker"], pila["handler"]
        inst = pila["spawn"]("conc-crons-serie")
        await inst.wait_ready()
        sesion, token = await _sesion_teams(pila, inst, "a", "aad-usuario-2")
        ids = [
            await _cron(maker, pila["agentes"]["a"], f"serie-{n}", f"Informe {n} DEMORA=4 MARCA-SERIE{n}")
            for n in range(3)
        ]
        await _esperar(lambda: _alguna(handler, "MARCA-SERIE0", "MARCA-SERIE1", "MARCA-SERIE2"), 40, "disparo")
        respuesta = await _chat(inst.base, sesion, token, "Mientras tanto MARCA-CHATSERIE DEMORA=1")
        assert respuesta["type"] == "done", respuesta

        async def _todas():
            listas = [await _tareas_de_cron(maker, i) for i in ids]
            return listas if all(len(x) == 1 and x[0].status == "completed" for x in listas) else None

        await _esperar(_todas, 90, "los tres crons completados")
        await asyncio.sleep(7)
        listas = [await _tareas_de_cron(maker, i) for i in ids]
        assert [len(x) for x in listas] == [1, 1, 1], "un disparo se duplicó en ticks posteriores"
        tareas = sorted((x[0] for x in listas), key=lambda t: t.started_at)
        for anterior, siguiente in zip(tareas, tareas[1:], strict=False):
            assert siguiente.started_at >= anterior.completed_at, "dos crons del mismo agente se solaparon"
        assert len({t.metadata_json["scheduled_fire_key"] for t in tareas}) == 3
        for n in range(3):
            assert len(_llamadas(handler, f"MARCA-SERIE{n}")) == 1
        assert _maximo_simultaneo(handler, ("MARCA-SERIE",)) == 1
        for posicion, t in enumerate(tareas):
            _medir("2", f"cron serie #{posicion + 1}", t, handler)
        medida_chat = _medir("2", "chat durante la serie", (await _tareas_con(maker, "MARCA-CHATSERIE"))[0], handler)
        assert medida_chat["cola_s"] < 2.0

    async def test_cron_manual_por_api_y_por_hq_schedule_run_durante_una_conversacion(self, pila):
        maker, handler = pila["maker"], pila["handler"]
        agente = pila["agentes"]["a"]
        inst = pila["spawn"]("conc-cron-manual")
        await inst.wait_ready()
        headers = await _login(inst.base)
        api_id = await _cron(maker, agente, "manual-api", "Ejecuta MARCA-MANUALAPI DEMORA=2", vencido=False)
        hq_id = await _cron(maker, agente, "manual-hq", "Ejecuta MARCA-MANUALHQ DEMORA=3", vencido=False)
        sesion, token = await _sesion_teams(pila, inst, "a", "aad-usuario-3")
        conversacion = asyncio.create_task(_chat(inst.base, sesion, token, "Trabajo largo MARCA-CHATMANUAL DEMORA=14"))
        await _esperar(lambda: _llamada(handler, "MARCA-CHATMANUAL"), 30, "chat en curso")
        chat = (await _tareas_con(maker, "MARCA-CHATMANUAL"))[0]

        await _api("POST", inst.base, f"/scheduled-tasks/{api_id}/run", headers)
        cabeceras_agente = {"X-HermesHQ-Agent-ID": agente, "X-HermesHQ-Agent-Token": _token_agente(agente)}
        async with httpx.AsyncClient(timeout=15) as c:
            r1 = await c.post(
                f"{inst.base}/api/internal/agents/self/schedules/{hq_id}/run",
                headers=cabeceras_agente,
                json={"task_id": chat.id},
            )
            r2 = await c.post(
                f"{inst.base}/api/internal/agents/self/schedules/{hq_id}/run",
                headers=cabeceras_agente,
                json={"task_id": chat.id},
            )
        assert r1.status_code == 202, r1.text
        assert r2.status_code == 409, "hq_schedule_run duplicó una ejecución en curso"

        async def _manuales():
            a, h = await _tareas_de_cron(maker, api_id), await _tareas_de_cron(maker, hq_id)
            if len(a) == 1 and len(h) == 1 and a[0].status == h[0].status == "completed":
                return a[0], h[0]
            return None

        por_api, por_hq = await _esperar(_manuales, 40, "crons manuales completados")
        assert (await _tarea(maker, chat.id)).status == "running", "los crons manuales esperaron al chat"
        assert por_api.metadata_json.get("manual_trigger") and por_hq.metadata_json.get("manual_trigger")
        assert por_hq.metadata_json.get("origin_task_id") == chat.id
        respuesta = await asyncio.wait_for(conversacion, 60)
        assert respuesta["type"] == "done", respuesta
        chat = await _tarea(maker, chat.id)
        assert chat.completed_at > max(por_api.completed_at, por_hq.completed_at)
        assert len(_llamadas(handler, "MARCA-MANUALAPI")) == 1 and len(_llamadas(handler, "MARCA-MANUALHQ")) == 1
        _medir("4", "cron manual API", por_api, handler)
        _medir("4", "cron manual hq_schedule_run", por_hq, handler)
        _medir("4", "chat en curso", chat, handler)


class TestCronsDeVariosAgentes:
    async def test_crons_de_tres_agentes_corren_en_paralelo(self, pila):
        maker, handler = pila["maker"], pila["handler"]
        inst = pila["spawn"]("conc-varios-agentes")
        await inst.wait_ready()
        ids = [
            await _cron(maker, pila["agentes"][letra], f"cron-{letra}", f"Informe DEMORA=6 MARCA-PARALELO{letra}")
            for letra in "abc"
        ]

        async def _todas():
            listas = [await _tareas_de_cron(maker, i) for i in ids]
            return listas if all(len(x) == 1 and x[0].status == "completed" for x in listas) else None

        listas = await _esperar(_todas, 60, "crons de tres agentes")
        tareas = [x[0] for x in listas]
        assert max(t.started_at for t in tareas) < min(t.completed_at for t in tareas), "no corrieron en paralelo"
        assert _maximo_simultaneo(handler, ("MARCA-PARALELO",)) == 3
        for letra, t in zip("abc", tareas, strict=True):
            assert len(_llamadas(handler, f"MARCA-PARALELO{letra}")) == 1
            _medir("3", f"cron agente {letra}", t, handler)


MARGEN_ESPERA_NO_INTERACTIVA_S = 10
UMBRAL_ADMISION_CHAT_S = 2.5
UMBRAL_ADMISION_CRON_CON_CUPO_S = 9
PERIODO_MINIMO_CHAT_S = 6.5
TOPE_EFECTIVO_CON_CUPO_3 = 2


async def _sembrar_segundo_plano(maker, agent_id: str, marca: str, demora: int) -> str:
    dueno = await _id_admin(maker)
    async with maker() as s:
        tarea = Task(
            agent_id=agent_id,
            title="Trabajo en segundo plano",
            prompt=f"Procesa el lote DEMORA={demora} {marca}",
            created_by_user_id=dueno,
            metadata_json={"background_job": True, "source": "background_job", "thread_user_id": dueno},
        )
        s.add(tarea)
        await s.commit()
        return tarea.id


class _Muestreo:
    def __init__(self, pila, prefijos: tuple[str, ...]):
        self.maker, self.handler, self.prefijos = pila["maker"], pila["handler"], prefijos
        self.pendientes = pila["pendientes"]
        self.maximo = {"no_interactivas": 0, "crons": 0, "segundo_plano": 0, "en_modelo": 0}
        self.muestras = 0
        self._tarea: asyncio.Task | None = None

    def iniciar(self) -> None:
        self._tarea = asyncio.create_task(self._ciclo())
        self.pendientes.append(self._tarea)

    async def _ciclo(self) -> None:
        while True:
            async with self.maker() as s:
                filas = (await s.execute(select(Task.metadata_json).where(Task.status == "running"))).scalars().all()
            crons = sum(1 for m in filas if (m or {}).get("scheduled"))
            fondo = sum(1 for m in filas if (m or {}).get("background_job") and not (m or {}).get("scheduled"))
            en_modelo = sum(
                1
                for r in list(self.handler.registros)
                if r["fin"] is None and any(m.startswith(p) for m in r["marcas"] for p in self.prefijos)
            )
            actual = {"no_interactivas": crons + fondo, "crons": crons, "segundo_plano": fondo, "en_modelo": en_modelo}
            self.maximo = {k: max(v, actual[k]) for k, v in self.maximo.items()}
            self.muestras += 1
            await asyncio.sleep(0.2)

    async def detener(self) -> dict:
        assert self._tarea is not None
        if self._tarea.done():
            self._tarea.result()
        self._tarea.cancel()
        try:
            await self._tarea
        except asyncio.CancelledError:
            pass
        return {**self.maximo, "muestras": self.muestras}


async def _en_curso(handler, marcas: tuple[str, ...]) -> str | None:
    for r in list(handler.registros):
        if r["fin"] is None:
            for m in marcas:
                if m in r["marcas"]:
                    return m
    return None


def _esperas_excedidas(tareas: list[Task], cupo: int, margen: float, evaluar: list[Task] | None = None) -> list[dict]:
    ordenadas = sorted(tareas, key=lambda t: t.started_at)
    excedidas = []
    juzgar = {x.id for x in (evaluar or tareas)}
    for i, t in enumerate(ordenadas):
        if t.id not in juzgar:
            continue
        fines = sorted(x.completed_at for x in ordenadas[:i] if x.completed_at > t.queued_at)
        libre = t.queued_at if len(fines) < cupo else max(t.queued_at, fines[len(fines) - cupo])
        exceso = (t.started_at - libre).total_seconds() - margen
        if exceso > 0:
            excedidas.append({"tarea": t.prompt[:60], "espera_tras_cupo_libre_s": round(exceso + margen, 2)})
    return excedidas


def _percentil(valores: list[float], p: float) -> float:
    ordenados = sorted(valores)
    return ordenados[min(len(ordenados) - 1, max(0, round(p * (len(ordenados) - 1))))]


def _latencias(caso: str, filas: list[dict]) -> dict:
    tabla: dict[str, dict] = {}
    for fila in filas:
        tabla.setdefault(fila["tipo"], {"n": 0, **{c: [] for c in ("cola_s", "hasta_llm_s", "total_s")}})
        tabla[fila["tipo"]]["n"] += 1
        for campo in ("cola_s", "hasta_llm_s", "total_s"):
            if fila[campo] is not None:
                tabla[fila["tipo"]][campo].append(fila[campo])
    resumen = {
        tipo: {
            "n": datos["n"],
            **{
                campo: {"p50": _percentil(v, 0.5), "p95": _percentil(v, 0.95), "max": max(v)}
                for campo, v in datos.items()
                if campo != "n" and v
            },
        }
        for tipo, datos in tabla.items()
    }
    print("LATENCIAS", json.dumps({"caso": caso, "por_tipo": resumen}, ensure_ascii=False))
    return resumen


async def _chat_en_curso(maker, agent_id: str) -> str | None:
    async with maker() as s:
        filas = await s.execute(select(Task).where(Task.agent_id == agent_id, Task.status == "running"))
        for t in filas.scalars().all():
            if (t.metadata_json or {}).get("source") == "public_chat":
                return t.id
    return None


async def _trabajo_desde_el_chat(inst: _Instance, maker, agent_id: str, instrucciones: str) -> str:
    cabeceras = {"X-HermesHQ-Agent-ID": agent_id, "X-HermesHQ-Agent-Token": _token_agente(agent_id)}
    limite = time.monotonic() + 30
    while time.monotonic() < limite:
        origen = await _chat_en_curso(maker, agent_id)
        if origen:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{inst.base}/api/internal/agents/self/background-jobs",
                    headers=cabeceras,
                    json={"task_id": origen, "title": "Lote de saturación", "instructions": instrucciones},
                )
            if r.status_code == 201:
                return r.json()["job_id"]
            assert r.status_code == 403, r.text
        await asyncio.sleep(0.1)
    raise AssertionError("no se pudo abrir el trabajo en segundo plano desde una conversación viva")


class _ChatContinuo:
    def __init__(self, pila, inst: _Instance, sesiones: dict, duracion: float, demora: int, prefijo: str):
        self.inst, self.sesiones, self.demora, self.prefijo = inst, sesiones, demora, prefijo
        self.fin_monotono = time.monotonic() + duracion
        self.fin_utc = datetime.now(UTC) + timedelta(seconds=duracion)
        self.atendidos = {x: 0 for x in sesiones}
        self.usuarios = [asyncio.create_task(self._usuario(x)) for x in sesiones]
        pila["pendientes"].extend(self.usuarios)

    async def _usuario(self, letra: str) -> None:
        sesion, token = self.sesiones[letra]
        n = 0
        ip = f"10.77.0.{ord(letra)}"
        while time.monotonic() < self.fin_monotono:
            envio = time.monotonic()
            contenido = f"Pregunta {self.prefijo}{letra}{n} DEMORA={self.demora}"
            respuesta = await _chat(self.inst.base, sesion, token, contenido, ip)
            assert respuesta["type"] == "done", respuesta
            self.atendidos[letra] += 1
            n += 1
            await asyncio.sleep(max(0.0, PERIODO_MINIMO_CHAT_S - (time.monotonic() - envio)))

    async def terminar(self) -> dict:
        await asyncio.gather(*self.usuarios)
        return self.atendidos


async def _todas_completadas(maker, ids_tarea: list[str], ids_cron: list[str]):
    tareas = [await _tarea(maker, i) for i in ids_tarea] + [
        (await _tareas_de_cron(maker, c) or [None])[0] for c in ids_cron
    ]
    return tareas if all(t is not None and t.status == "completed" for t in tareas) else None


class TestSaturacionDelLimiteGlobal:
    @pytest.mark.parametrize(
        ("reserva", "chat_debe_esperar"),
        [pytest.param("2", False, id="reserva-por-defecto"), pytest.param("0", True, id="control-sin-reserva")],
    )
    async def test_a_chat_admitido_con_no_interactivas_saturando_su_cupo(self, pila, reserva, chat_debe_esperar):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        total = 3
        cupo_no_interactivo = total - int(reserva)
        entorno = {"CONCURRENCY_SEMAPHORE": str(total), "CONCURRENCY_INTERACTIVE_RESERVE": reserva}
        caso = f"A reserva={reserva}"
        preparacion = pila["spawn"](f"conc-sat-prep-{reserva}", entorno)
        await preparacion.wait_ready()
        preparacion.kill_group()
        fondo = [await _sembrar_segundo_plano(maker, agentes[x], f"MARCA-FONDO{x}", 15) for x in "bc"]
        crons = [await _cron(maker, agentes[x], f"cron-sat-{x}", f"Informe DEMORA=15 MARCA-CRONSAT{x}") for x in "ae"]
        marcas_no_interactivas = ("MARCA-FONDOb", "MARCA-FONDOc", "MARCA-CRONSATa", "MARCA-CRONSATe")
        muestreo = _Muestreo(pila, ("MARCA-FONDO", "MARCA-CRONSAT"))
        muestreo.iniciar()
        inst = pila["spawn"](f"conc-sat-{reserva}", entorno)
        await inst.wait_ready()

        primera = await _esperar(lambda: _en_curso(handler, marcas_no_interactivas), 40, "cupo no interactivo ocupado")
        await asyncio.sleep(3)
        oleadas = [("f", primera[-1])]
        respuestas: dict[str, dict] = {}

        async def _oleada(letras: tuple[str, ...], n: int) -> None:
            sesiones = [await _sesion_teams(pila, inst, x, f"aad-sat-{n}-{x}") for x in letras]
            resultado = await asyncio.gather(
                *[
                    _chat(inst.base, s, t, f"Consulta MARCA-CHATSAT{n}{x} DEMORA=1", f"10.78.{n}.{ord(x)}")
                    for x, (s, t) in zip(letras, sesiones, strict=True)
                ]
            )
            for x, r in zip(letras, resultado, strict=True):
                respuestas[f"MARCA-CHATSAT{n}{x}"] = r

        await _oleada(oleadas[0], 1)
        colas_oleada_1 = [(await _tareas_con(maker, f"MARCA-CHATSAT1{x}"))[0] for x in oleadas[0]]
        colas_oleada_1 = [(t.started_at - t.queued_at).total_seconds() for t in colas_oleada_1]
        if not chat_debe_esperar:
            assert max(colas_oleada_1) < UMBRAL_ADMISION_CHAT_S, colas_oleada_1
        cron_en_curso = await _esperar(
            lambda: _en_curso(handler, ("MARCA-CRONSATa", "MARCA-CRONSATe")),
            120,
            "un cron con el modelo en curso",
        )
        oleadas.append((cron_en_curso[-1], "d"))
        await _oleada(oleadas[1], 2)
        assert all(r["type"] == "done" for r in respuestas.values()), respuestas

        no_interactivas = await _esperar(
            lambda: _todas_completadas(maker, fondo, crons), 200, "crons y segundo plano completados"
        )
        maximos = await muestreo.detener()
        filas = []
        for marca in respuestas:
            chat = (await _tareas_con(maker, marca))[0]
            filas.append(_medir(caso, f"chat {marca[-2:]}", chat, handler, tipo="chat"))
        for etiqueta, t in zip(["fondo b", "fondo c", "cron a", "cron e"], no_interactivas, strict=True):
            filas.append(_medir(caso, etiqueta, t, handler, tipo="cron" if "cron" in etiqueta else "segundo plano"))
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "maximos_muestreados": maximos, "oleadas": oleadas}))

        for marca in marcas_no_interactivas:
            assert len(_llamadas(handler, marca)) == 1, marca
        for marca in respuestas:
            assert len(_llamadas(handler, marca)) == 1, marca
        assert maximos["muestras"] >= 100, maximos
        assert maximos["no_interactivas"] == maximos["en_modelo"] == cupo_no_interactivo, maximos
        assert _maximo_simultaneo(handler, ("MARCA-FONDO", "MARCA-CRONSAT")) == cupo_no_interactivo
        colas_chat = [f["cola_s"] for f in filas if f["tipo"] == "chat"]
        if chat_debe_esperar:
            assert max(colas_chat) > 5, "el control sin reserva debería reproducir la espera"
            return
        assert max(colas_chat) < UMBRAL_ADMISION_CHAT_S, filas
        assert _esperas_excedidas(no_interactivas, cupo_no_interactivo, MARGEN_ESPERA_NO_INTERACTIVA_S) == []
        cron_del_agente = no_interactivas[2 if cron_en_curso[-1] == "a" else 3]
        chat_mismo_agente = (await _tareas_con(maker, f"MARCA-CHATSAT2{cron_en_curso[-1]}"))[0]
        assert cron_del_agente.started_at < chat_mismo_agente.queued_at
        assert chat_mismo_agente.completed_at < cron_del_agente.completed_at, "el chat esperó al cron de su agente"

    async def test_b_crons_y_segundo_plano_progresan_con_chat_continuo(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "B chat continuo"
        inst = pila["spawn"]("conc-sin-inanicion", {"CONCURRENCY_SEMAPHORE": "2"})
        await inst.wait_ready()
        sesiones = {x: await _sesion_teams(pila, inst, x, f"aad-continuo-{x}") for x in "abc"}
        muestreo = _Muestreo(pila, ("MARCA-CRONCONT", "MARCA-TRABAJO"))
        muestreo.iniciar()
        continuo = _ChatContinuo(pila, inst, sesiones, 80, 2, "MARCA-CONTINUO")
        await asyncio.sleep(6)
        crons = [
            await _cron(maker, agentes[x], f"cron-continuo-{x}", f"Informe DEMORA=4 MARCA-CRONCONT{x}") for x in "de"
        ]
        trabajos = [
            await _trabajo_desde_el_chat(inst, maker, agentes[x], f"Procesa el lote DEMORA=4 MARCA-TRABAJO{x}")
            for x in "ab"
        ]
        no_interactivas = await _esperar(
            lambda: _todas_completadas(maker, trabajos, crons), 150, "crons y segundo plano con chat continuo"
        )
        ultimo_inicio = max(t.started_at for t in no_interactivas)
        atendidos = await continuo.terminar()
        maximos = await muestreo.detener()

        filas = [
            _medir(caso, e, t, handler, tipo="segundo plano" if "trabajo" in e else "cron")
            for e, t in zip(["trabajo a", "trabajo b", "cron d", "cron e"], no_interactivas, strict=True)
        ]
        for x in "abc":
            for chat in await _tareas_con(maker, f"MARCA-CONTINUO{x}"):
                filas.append(_medir(caso, f"chat {x}", chat, handler, tipo="chat"))
        _latencias(caso, filas)
        excedidas = _esperas_excedidas(no_interactivas, 1, MARGEN_ESPERA_NO_INTERACTIVA_S)
        print(
            "MEDICION",
            json.dumps({"caso": caso, "atendidos": atendidos, "maximos_muestreados": maximos, "excedidas": excedidas}),
        )

        assert excedidas == [], excedidas
        assert ultimo_inicio < continuo.fin_utc - timedelta(seconds=5), "el chat continuo terminó antes"
        assert maximos["no_interactivas"] == 1, maximos
        for marca in ("MARCA-CRONCONTd", "MARCA-CRONCONTe", "MARCA-TRABAJOa", "MARCA-TRABAJOb"):
            assert len(_llamadas(handler, marca)) == 1, marca
        assert min(atendidos.values()) >= 5, atendidos
        assert all(f["estado"] == "completed" for f in filas), filas

    @pytest.mark.parametrize(
        "tope",
        [
            pytest.param("2", id="tope-deja-cupo-a-crons"),
            pytest.param("3", id="tope-igual-cupo"),
            pytest.param("9", id="tope-mayor-que-el-total"),
        ],
    )
    async def test_b_tope_de_segundo_plano_deja_cupo_a_los_crons(self, pila, tope):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = f"B tope={tope}"
        entorno = {
            "CONCURRENCY_SEMAPHORE": "4",
            "CONCURRENCY_INTERACTIVE_RESERVE": "1",
            "CONCURRENCY_BACKGROUND_MAX": tope,
        }
        preparacion = pila["spawn"](f"conc-tope-prep-{tope}", entorno)
        await preparacion.wait_ready()
        preparacion.kill_group()
        lotes = [await _sembrar_segundo_plano(maker, agentes[x], f"MARCA-LOTE{x}", 30) for x in "bcd"]
        muestreo = _Muestreo(pila, ("MARCA-LOTE", "MARCA-CRONTOPE"))
        muestreo.iniciar()
        inst = pila["spawn"](f"conc-tope-{tope}", entorno)
        await inst.wait_ready()

        async def _lotes_en_curso():
            en_modelo = [
                r
                for r in list(handler.registros)
                if r["fin"] is None and any(m.startswith("MARCA-LOTE") for m in r["marcas"])
            ]
            return len(en_modelo) >= TOPE_EFECTIVO_CON_CUPO_3

        await _esperar(_lotes_en_curso, 40, "segundo plano llenando su tope")
        sesiones = {x: await _sesion_teams(pila, inst, x, f"aad-tope-{x}") for x in "ef"}
        continuo = _ChatContinuo(pila, inst, sesiones, 40, 2, "MARCA-TOPECHAT")
        await asyncio.sleep(3)
        cron = await _cron(maker, agentes["a"], "cron-con-tope", "Informe DEMORA=3 MARCA-CRONTOPEa")
        todas = await _esperar(lambda: _todas_completadas(maker, lotes, [cron]), 200, "lotes y cron completados")
        atendidos = await continuo.terminar()
        maximos = await muestreo.detener()

        filas = [
            _medir(caso, e, t, handler, tipo="cron" if "cron" in e else "segundo plano")
            for e, t in zip(["lote b", "lote c", "lote d", "cron a"], todas, strict=True)
        ]
        for x in "ef":
            for chat in await _tareas_con(maker, f"MARCA-TOPECHAT{x}"):
                filas.append(_medir(caso, f"chat {x}", chat, handler, tipo="chat"))
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "atendidos": atendidos, "maximos_muestreados": maximos}))

        for marca in ("MARCA-LOTEb", "MARCA-LOTEc", "MARCA-LOTEd", "MARCA-CRONTOPEa"):
            assert len(_llamadas(handler, marca)) == 1, marca
        assert min(atendidos.values()) >= 4, atendidos
        medida_cron = filas[3]
        assert medida_cron["cola_s"] < UMBRAL_ADMISION_CRON_CON_CUPO_S, medida_cron
        assert maximos["segundo_plano"] == TOPE_EFECTIVO_CON_CUPO_3, maximos
        assert maximos["no_interactivas"] <= 3, maximos
        assert todas[3].completed_at < min(t.completed_at for t in todas[:3] if t.started_at < todas[3].queued_at)
        assert _esperas_excedidas(todas[:3], 2, MARGEN_ESPERA_NO_INTERACTIVA_S) == []
        assert _esperas_excedidas(todas, 3, MARGEN_ESPERA_NO_INTERACTIVA_S, evaluar=[todas[3]]) == []


async def _tarea_terminada_de_cron(maker, schedule_id: str):
    tareas = await _tareas_de_cron(maker, schedule_id)
    return tareas[0] if tareas and tareas[0].status in ("completed", "failed", "cancelled") else None


def _maximo_simultaneo(handler, prefijos: tuple[str, ...]) -> int:
    eventos = []
    for r in list(handler.registros):
        if any(m.startswith(p) for m in r["marcas"] for p in prefijos):
            eventos.append((r["inicio"], 1))
            eventos.append((r["fin"] or time.time(), -1))
    maximo = actual = 0
    for _, delta in sorted(eventos, key=lambda e: (e[0], e[1])):
        actual += delta
        maximo = max(maximo, actual)
    return maximo


class TestReinicioYCancelacion:
    async def test_reinicio_real_con_cron_y_chat_en_curso(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        a = pila["spawn"]("conc-reinicio-a")
        await a.wait_ready()
        sesion, token = await _sesion_teams(pila, a, "a", "aad-usuario-reinicio")
        clave = pila["claves"]["aad-usuario-reinicio"]
        assert await _sondeo_del_bridge(a.base, clave) == []
        cron_id = await _cron(maker, agentes["a"], "cron-reinicio", "Informe DEMORA=12 MARCA-CRONREIN")
        await _esperar(lambda: _llamada(handler, "MARCA-CRONREIN"), 40, "cron en curso")
        conversacion = asyncio.create_task(_chat(a.base, sesion, token, "Trabajo MARCA-CHATREIN DEMORA=12"))
        await _esperar(lambda: _llamada(handler, "MARCA-CHATREIN"), 30, "chat en curso")
        chat_id = (await _tareas_con(maker, "MARCA-CHATREIN"))[0].id
        original_cron = (await _tareas_de_cron(maker, cron_id))[0]

        a.kill_group()
        with pytest.raises(httpx.HTTPError):
            await asyncio.wait_for(conversacion, 30)
        b = pila["spawn"]("conc-reinicio-b")
        await b.wait_ready()

        entregadas: list[dict] = []

        async def _ambos():
            entregadas.extend(await _sondeo_del_bridge(b.base, clave))
            tareas = await _tareas_de_cron(maker, cron_id)
            chat = await _tarea(maker, chat_id)
            terminado = ("completed", "failed")
            if len(tareas) >= 2 and tareas[-1].status in terminado and chat.status in terminado:
                return tareas, chat
            return None

        tareas, chat = await _esperar(_ambos, 120, "cron reintentado y chat recuperado")
        for _ in range(6):
            entregadas.extend(await _sondeo_del_bridge(b.base, clave))
            await asyncio.sleep(1)
        tareas = await _tareas_de_cron(maker, cron_id)
        chat = await _tarea(maker, chat_id)
        assert [t.id for t in tareas][:1] == [original_cron.id]
        assert len(tareas) == 2, "el cron se reintentó más de una vez"
        assert tareas[0].status == "failed" and tareas[1].status == "completed"
        assert tareas[1].metadata_json["retry_of"] == original_cron.id
        assert len(_llamadas(handler, "MARCA-CRONREIN")) == 2
        assert chat.status == "completed"
        assert chat.metadata_json.get("recovery_reintentos") == 1
        assert len(await _tareas_con(maker, "MARCA-CHATREIN")) == 1
        assert len(_llamadas(handler, "MARCA-CHATREIN")) == 2
        async with maker() as s:
            avisos = (
                await s.execute(
                    text("SELECT title, body FROM public_chat_notifications WHERE task_id = :t"), {"t": chat_id}
                )
            ).all()
        print("MEDICION", json.dumps({"caso": "7", "avisos_chat": [list(x) for x in avisos]}, ensure_ascii=False))
        assert len(avisos) == 1, (
            "la respuesta del chat recuperado no llega al usuario: su stream murió con la instancia"
        )
        assert "MARCA-CHATREIN" in avisos[0][1]
        al_bridge = {n["id"]: n for n in entregadas if n.get("task_id") == chat_id}
        assert len(al_bridge) == 1 and "MARCA-CHATREIN" in next(iter(al_bridge.values()))["body"]
        _medir("7", "cron original (interrumpido)", tareas[0], handler)
        _medir("7", "cron reintento", tareas[1], handler)
        _medir("7", "chat recuperado", chat, handler)

    async def test_cancelar_una_ejecucion_no_afecta_a_la_otra(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        inst = pila["spawn"]("conc-cancelacion")
        await inst.wait_ready()
        headers = await _login(inst.base)
        sesion, token = await _sesion_teams(pila, inst, "a", "aad-usuario-cancela")

        cron_id = await _cron(maker, agentes["a"], "cron-cancelado", "Informe DEMORA=20 MARCA-CRONCANC")
        await _esperar(lambda: _llamada(handler, "MARCA-CRONCANC"), 40, "cron en curso")
        cron = (await _tareas_de_cron(maker, cron_id))[0]
        conversacion = asyncio.create_task(_chat(inst.base, sesion, token, "Sigue MARCA-CHATSIGUE DEMORA=6"))
        await _esperar(lambda: _llamada(handler, "MARCA-CHATSIGUE"), 30, "chat en curso")
        await _api("POST", inst.base, f"/tasks/{cron.id}/cancel", headers)
        respuesta = await asyncio.wait_for(conversacion, 60)
        assert respuesta["type"] == "done" and "MARCA-CHATSIGUE" in respuesta["content"], respuesta
        cron = await _esperar(lambda: _terminada(maker, cron.id), 30, "cron cancelado")
        assert cron.status == "cancelled"
        _medir("8", "chat (cron cancelado)", (await _tareas_con(maker, "MARCA-CHATSIGUE"))[0], handler)

        segundo = await _cron(maker, agentes["a"], "cron-sigue", "Informe DEMORA=8 MARCA-CRONSIGUE")
        await _esperar(lambda: _llamada(handler, "MARCA-CRONSIGUE"), 40, "segundo cron en curso")
        conversacion = asyncio.create_task(_chat(inst.base, sesion, token, "Largo MARCA-CHATCANC DEMORA=20"))
        await _esperar(lambda: _llamada(handler, "MARCA-CHATCANC"), 30, "chat largo en curso")
        chat = (await _tareas_con(maker, "MARCA-CHATCANC"))[0]
        await _api("POST", inst.base, f"/tasks/{chat.id}/cancel", headers)
        respuesta = await asyncio.wait_for(conversacion, 30)
        assert respuesta["type"] == "done" and "cancelada" in respuesta["content"], respuesta
        cron2 = await _esperar(lambda: _tarea_terminada_de_cron(maker, segundo), 40, "segundo cron")
        assert cron2.status == "completed"
        assert (await _tarea(maker, chat.id)).status == "cancelled"
        assert len(_llamadas(handler, "MARCA-CRONSIGUE")) == 1
        _medir("8", "cron (chat cancelado)", cron2, handler)


UMBRAL_CRON_CON_CUPO_PROPIO_S = 5
UMBRAL_CRON_SIN_ESPERA_S = 2.5
UMBRAL_LIBERACION_TRAS_CANCELAR_S = 5


class _MuestreoGlobal:
    def __init__(self, pila):
        self.maker, self.pendientes = pila["maker"], pila["pendientes"]
        self.maximo = {"total": 0, "chat": 0, "cron": 0, "segundo_plano": 0, "no_interactivas": 0}
        self.muestras = 0
        self._tarea: asyncio.Task | None = None

    def iniciar(self) -> "_MuestreoGlobal":
        self._tarea = asyncio.create_task(self._ciclo())
        self.pendientes.append(self._tarea)
        return self

    async def _ciclo(self) -> None:
        while True:
            async with self.maker() as s:
                filas = (await s.execute(select(Task.metadata_json).where(Task.status == "running"))).scalars().all()
            crons = sum(1 for m in filas if (m or {}).get("scheduled"))
            fondo = sum(1 for m in filas if (m or {}).get("background_job") and not (m or {}).get("scheduled"))
            actual = {
                "total": len(filas),
                "chat": len(filas) - crons - fondo,
                "cron": crons,
                "segundo_plano": fondo,
                "no_interactivas": crons + fondo,
            }
            self.maximo = {k: max(v, actual[k]) for k, v in self.maximo.items()}
            self.muestras += 1
            await asyncio.sleep(0.2)

    async def detener(self) -> dict:
        assert self._tarea is not None
        if self._tarea.done():
            self._tarea.result()
        self._tarea.cancel()
        try:
            await self._tarea
        except asyncio.CancelledError:
            pass
        return {**self.maximo, "muestras": self.muestras}


async def _preparar_base(pila, nombre: str, entorno: dict) -> None:
    preparacion = pila["spawn"](nombre, entorno)
    await preparacion.wait_ready()
    preparacion.kill_group()


async def _estado_de(maker, task_id: str) -> str:
    return (await _tarea(maker, task_id)).status


def _admision(tarea: Task, campo: str) -> datetime:
    return datetime.fromisoformat(tarea.metadata_json["admision"][campo])


def _admitidas_mientras_esperaba(tareas: list[Task], espera: Task) -> list[str]:
    desde, hasta = _admision(espera, "en_cola_at"), _admision(espera, "admitida_at")
    return [t.prompt[:50] for t in tareas if desde < _admision(t, "admitida_at") < hasta]


class TestInanicionNoInteractiva:
    async def test_cupo_no_interactivo_uno_con_tope_alto(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "C1 total=3 reserva=2 tope=3"
        entorno = {
            "CONCURRENCY_SEMAPHORE": "3",
            "CONCURRENCY_INTERACTIVE_RESERVE": "2",
            "CONCURRENCY_BACKGROUND_MAX": "3",
        }
        await _preparar_base(pila, "c1-prep", entorno)
        lotes = [await _sembrar_segundo_plano(maker, agentes[x], f"MARCA-CUNOL{x}", 20) for x in "bcd"]
        muestreo = _MuestreoGlobal(pila).iniciar()
        inst = pila["spawn"]("c1", entorno)
        await inst.wait_ready()
        await _esperar(lambda: _alguna(handler, "MARCA-CUNOLb", "MARCA-CUNOLc", "MARCA-CUNOLd"), 40, "lote en curso")
        await asyncio.sleep(3)
        cron = await _cron(maker, agentes["a"], "cron-c1", "Informe DEMORA=3 MARCA-CUNOCRONa")
        sesiones = [await _sesion_teams(pila, inst, x, f"aad-c1-{x}") for x in "ef"]
        await _esperar(lambda: _tareas_de_cron(maker, cron), 30, "cron encolado")
        respuestas = await asyncio.gather(
            *[
                _chat(inst.base, s, t, f"Consulta MARCA-CUNOCHAT{x} DEMORA=1", f"10.79.1.{ord(x)}")
                for x, (s, t) in zip("ef", sesiones, strict=True)
            ]
        )
        assert all(r["type"] == "done" for r in respuestas), respuestas
        todas = await _esperar(lambda: _todas_completadas(maker, lotes, [cron]), 200, "lotes y cron completados")
        maximos = await muestreo.detener()
        filas = [
            _medir(caso, e, t, handler, tipo="cron" if "cron" in e else "segundo plano")
            for e, t in zip(["lote b", "lote c", "lote d", "cron a"], todas, strict=True)
        ]
        for x in "ef":
            filas.append(
                _medir(caso, f"chat {x}", (await _tareas_con(maker, f"MARCA-CUNOCHAT{x}"))[0], handler, "chat")
            )
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "maximos_muestreados": maximos}))

        assert max(f["cola_s"] for f in filas if f["tipo"] == "chat") < UMBRAL_ADMISION_CHAT_S, filas
        assert maximos["no_interactivas"] == 1 and maximos["total"] <= 3, maximos
        tarea_cron = todas[3]
        en_vuelo = [t for t in todas[:3] if t.started_at < tarea_cron.queued_at < t.completed_at]
        assert len(en_vuelo) == 1, "el cron debía encontrar un solo lote en curso"
        assert _admitidas_mientras_esperaba(todas[:3], tarea_cron) == [], "un lote en cola se adelantó al cron"
        espera_tras_liberar = (tarea_cron.started_at - en_vuelo[0].completed_at).total_seconds()
        assert espera_tras_liberar < UMBRAL_CRON_CON_CUPO_PROPIO_S, espera_tras_liberar
        assert _esperas_excedidas(todas, 1, MARGEN_ESPERA_NO_INTERACTIVA_S) == []
        for marca in ("MARCA-CUNOLb", "MARCA-CUNOLc", "MARCA-CUNOLd", "MARCA-CUNOCRONa"):
            assert len(_llamadas(handler, marca)) == 1, marca

    async def test_total_uno(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "total=1"
        entorno = {"CONCURRENCY_SEMAPHORE": "1"}
        await _preparar_base(pila, "t1-prep", entorno)
        lotes = [await _sembrar_segundo_plano(maker, agentes[x], f"MARCA-TUNOL{x}", 8) for x in "bc"]
        muestreo = _MuestreoGlobal(pila).iniciar()
        inst = pila["spawn"]("t1", entorno)
        await inst.wait_ready()
        await _esperar(lambda: _alguna(handler, "MARCA-TUNOLb", "MARCA-TUNOLc"), 40, "lote en curso")
        cron = await _cron(maker, agentes["a"], "cron-t1", "Informe DEMORA=3 MARCA-TUNOCRONa")
        await _esperar(lambda: _tareas_de_cron(maker, cron), 30, "cron encolado")
        sesion, token = await _sesion_teams(pila, inst, "e", "aad-t1-e")
        respuesta = await _chat(inst.base, sesion, token, "Consulta MARCA-TUNOCHATe DEMORA=1", "10.79.2.1")
        assert respuesta["type"] == "done", respuesta
        todas = await _esperar(lambda: _todas_completadas(maker, lotes, [cron]), 120, "lotes y cron completados")
        maximos = await muestreo.detener()
        chat = (await _tareas_con(maker, "MARCA-TUNOCHATe"))[0]
        filas = [
            _medir(caso, e, t, handler, tipo="cron" if "cron" in e else "segundo plano")
            for e, t in zip(["lote b", "lote c", "cron a"], todas, strict=True)
        ]
        filas.append(_medir(caso, "chat e", chat, handler, tipo="chat"))
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "maximos_muestreados": maximos}))

        assert maximos["total"] == 1, maximos
        assert _maximo_simultaneo(handler, ("MARCA-TUNO",)) == 1
        en_vuelo = [t for t in todas if t.started_at < chat.queued_at < t.completed_at]
        assert len(en_vuelo) == 1
        assert _admitidas_mientras_esperaba(todas, chat) == [], "un no interactivo se adelantó al chat"
        assert (chat.started_at - en_vuelo[0].completed_at).total_seconds() < UMBRAL_ADMISION_CHAT_S
        assert _esperas_excedidas([*todas, chat], 1, MARGEN_ESPERA_NO_INTERACTIVA_S) == []
        for marca in ("MARCA-TUNOLb", "MARCA-TUNOLc", "MARCA-TUNOCRONa", "MARCA-TUNOCHATe"):
            assert len(_llamadas(handler, marca)) == 1, marca
        with open(inst.log.name) as registro:
            texto = registro.read()
        assert "el chat pasa primero" in texto, "el arranque no avisó la normalización de la reserva"

    async def test_rafaga_de_diez_trabajos_y_dos_crons(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "rafaga total=4 reserva=1 tope=10"
        entorno = {
            "CONCURRENCY_SEMAPHORE": "4",
            "CONCURRENCY_INTERACTIVE_RESERVE": "1",
            "CONCURRENCY_BACKGROUND_MAX": "10",
        }
        await _preparar_base(pila, "raf-prep", entorno)
        lotes = []
        for n in range(10):
            letra = "bcdef"[n % 5]
            lotes.append(await _sembrar_segundo_plano(maker, agentes[letra], f"MARCA-RAFL{n}", 10))
        muestreo = _MuestreoGlobal(pila).iniciar()
        inst = pila["spawn"]("raf", entorno)
        await inst.wait_ready()

        async def _dos_lotes():
            return sum(1 for r in list(handler.registros) if r["fin"] is None and any("RAFL" in m for m in r["marcas"]))

        await _esperar(_dos_lotes, 40, "lotes en curso")
        await asyncio.sleep(2)
        crons = [await _cron(maker, agentes[x], f"cron-raf-{x}", f"Informe DEMORA=3 MARCA-RAFCRON{x}") for x in "ab"]
        sesion, token = await _sesion_teams(pila, inst, "c", "aad-raf-c")
        respuesta = await _chat(inst.base, sesion, token, "Consulta MARCA-RAFCHATc DEMORA=1", "10.79.3.1")
        assert respuesta["type"] == "done", respuesta
        todas = await _esperar(lambda: _todas_completadas(maker, lotes, crons), 240, "ráfaga completada")
        maximos = await muestreo.detener()
        filas = [_medir(caso, f"lote {n}", t, handler, tipo="segundo plano") for n, t in enumerate(todas[:10])]
        filas += [_medir(caso, f"cron {x}", t, handler, tipo="cron") for x, t in zip("ab", todas[10:], strict=True)]
        filas.append(_medir(caso, "chat c", (await _tareas_con(maker, "MARCA-RAFCHATc"))[0], handler, tipo="chat"))
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "maximos_muestreados": maximos}))

        colas_cron = sorted(f["cola_s"] for f in filas if f["tipo"] == "cron")
        assert colas_cron[0] < UMBRAL_CRON_SIN_ESPERA_S, "ningún cron encontró su cupo propio libre"
        for tarea_cron in todas[10:]:
            assert _admitidas_mientras_esperaba(todas[:10], tarea_cron) == [], "un lote en cola se adelantó a un cron"
        assert _esperas_excedidas(todas, 3, MARGEN_ESPERA_NO_INTERACTIVA_S, evaluar=todas[10:]) == []
        assert filas[-1]["cola_s"] < UMBRAL_ADMISION_CHAT_S, filas[-1]
        assert maximos["segundo_plano"] == 2, maximos
        assert maximos["no_interactivas"] <= 3 and maximos["total"] <= 4, maximos
        assert _esperas_excedidas(todas[:10], 2, MARGEN_ESPERA_NO_INTERACTIVA_S) == []
        for n in range(10):
            assert len(_llamadas(handler, f"MARCA-RAFL{n}")) == 1, n
        for x in "ab":
            assert len(_llamadas(handler, f"MARCA-RAFCRON{x}")) == 1, x

    async def test_cancelar_en_curso_y_en_cola_libera_el_cupo_y_sobrevive_al_reinicio(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "cancelacion total=3 reserva=2"
        entorno = {"CONCURRENCY_SEMAPHORE": "3", "CONCURRENCY_INTERACTIVE_RESERVE": "2"}
        await _preparar_base(pila, "canc-prep", entorno)
        marcas = {x: f"MARCA-CANCL{x}" for x in "bcd"}
        ids = {x: await _sembrar_segundo_plano(maker, agentes[x], marcas[x], 12) for x in "bcd"}
        inst = pila["spawn"]("canc", entorno)
        await inst.wait_ready()
        headers = await _login(inst.base)
        en_curso = (await _esperar(lambda: _en_curso(handler, tuple(marcas.values())), 40, "un lote en curso"))[-1]
        await asyncio.sleep(1)
        en_cola, siguiente = (x for x in "bcd" if x != en_curso)
        assert await _estado_de(maker, ids[en_cola]) == "queued"
        assert await _estado_de(maker, ids[siguiente]) == "queued"

        await _api("POST", inst.base, f"/tasks/{ids[en_cola]}/cancel", headers)
        momento = datetime.now(UTC)
        await _api("POST", inst.base, f"/tasks/{ids[en_curso]}/cancel", headers)

        async def _cancelada_en_base():
            return await _estado_de(maker, ids[en_cola]) == "cancelled"

        await _esperar(_cancelada_en_base, 10, "la cancelación de la tarea en cola llegó a la base")
        terminado = await _esperar(lambda: _terminada(maker, ids[siguiente]), 60, "el lote restante toma el cupo")
        assert (await _tarea(maker, ids[en_curso])).status == "cancelled"
        assert terminado.status == "completed"
        espera = (terminado.started_at - momento).total_seconds()
        cron = await _cron(maker, agentes["a"], "cron-canc", "Informe DEMORA=2 MARCA-CANCCRONa")
        tarea_cron = await _esperar(lambda: _tarea_terminada_de_cron(maker, cron), 60, "cron tras cancelar")
        _medir(caso, "lote cancelado en curso", await _tarea(maker, ids[en_curso]), handler, tipo="segundo plano")
        _medir(caso, "lote cancelado en cola", await _tarea(maker, ids[en_cola]), handler, tipo="segundo plano")
        _medir(caso, "lote restante", terminado, handler, tipo="segundo plano")
        _medir(caso, "cron tras cancelar", tarea_cron, handler, tipo="cron")
        print("MEDICION", json.dumps({"caso": caso, "espera_tras_cancelar_s": round(espera, 2)}))
        assert espera < UMBRAL_LIBERACION_TRAS_CANCELAR_S, espera
        assert tarea_cron.status == "completed"
        assert (tarea_cron.started_at - tarea_cron.queued_at).total_seconds() < UMBRAL_CRON_CON_CUPO_PROPIO_S

        inst.kill_group()
        nueva = pila["spawn"]("canc-b", entorno)
        await nueva.wait_ready()
        await asyncio.sleep(8)
        assert await _estado_de(maker, ids[en_cola]) == "cancelled"
        assert _llamadas(handler, marcas[en_cola]) == [], "la tarea cancelada en cola se ejecutó"
        assert len(_llamadas(handler, marcas[en_curso])) == 1
        assert len(_llamadas(handler, marcas[siguiente])) == 1
        async with maker() as s:
            vivas = (
                await s.execute(
                    select(Task.id).where(Task.status.in_(("queued", "running")), Task.id.in_(list(ids.values())))
                )
            ).all()
        assert vivas == []

    async def test_reinicio_con_trabajos_en_cola(self, pila):
        maker, handler, agentes = pila["maker"], pila["handler"], pila["agentes"]
        caso = "reinicio total=3 reserva=2"
        entorno = {"CONCURRENCY_SEMAPHORE": "3", "CONCURRENCY_INTERACTIVE_RESERVE": "2"}
        await _preparar_base(pila, "rein-prep", entorno)
        marcas = {x: f"MARCA-REINL{x}" for x in "bcd"}
        ids = {x: await _sembrar_segundo_plano(maker, agentes[x], marcas[x], 15) for x in "bcd"}
        a = pila["spawn"]("rein-a", entorno)
        await a.wait_ready()
        en_curso = (await _esperar(lambda: _en_curso(handler, tuple(marcas.values())), 40, "un lote en curso"))[-1]
        cron = await _cron(maker, agentes["a"], "cron-rein", "Informe DEMORA=3 MARCA-REINCRONa")
        await _esperar(lambda: _tareas_de_cron(maker, cron), 30, "cron encolado")
        await asyncio.sleep(1)
        en_cola = [x for x in "bcd" if x != en_curso]
        estados = [await _estado_de(maker, ids[x]) for x in en_cola] + [(await _tareas_de_cron(maker, cron))[0].status]
        assert estados == ["queued", "queued", "queued"], estados
        a.kill_group()

        muestreo = _MuestreoGlobal(pila).iniciar()
        b = pila["spawn"]("rein-b", entorno)
        await b.wait_ready()
        cola = [ids[x] for x in en_cola]
        todas = await _esperar(lambda: _todas_completadas(maker, cola, [cron]), 150, "cola retomada tras reinicio")
        resuelto = await _esperar(lambda: _terminada(maker, ids[en_curso]), 120, "lote interrumpido resuelto")
        await asyncio.sleep(2)
        maximos = await muestreo.detener()
        filas = [
            _medir(caso, e, t, handler, tipo="cron" if "cron" in e else "segundo plano")
            for e, t in zip([f"lote {en_cola[0]}", f"lote {en_cola[1]}", "cron a"], todas, strict=True)
        ]
        filas.append(_medir(caso, "lote interrumpido", resuelto, handler, tipo="segundo plano"))
        _latencias(caso, filas)
        print("MEDICION", json.dumps({"caso": caso, "maximos_muestreados": maximos, "interrumpido": resuelto.status}))

        assert maximos["no_interactivas"] == 1 and maximos["total"] <= 3, maximos
        for x in en_cola:
            assert len(_llamadas(handler, marcas[x])) == 1, x
        assert len(_llamadas(handler, "MARCA-REINCRONa")) == 1
        assert len(_llamadas(handler, marcas[en_curso])) <= 2
        tarea_cron = todas[2]
        assert tarea_cron.started_at < max(t.started_at for t in todas[:2]), "tras el reinicio el cron quedó al final"
        async with maker() as s:
            vivas = (
                await s.execute(
                    select(Task.id).where(
                        Task.status.in_(("queued", "running")), Task.id.in_([*ids.values(), tarea_cron.id])
                    )
                )
            ).all()
        assert vivas == []
