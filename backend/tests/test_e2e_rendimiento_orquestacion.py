import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx
import pytest
import test_e2e_reset_aislamiento as _reset_aislamiento
from conftest import requires_database
from test_e2e_http_recuperacion import _api, _login

stack_reset = _reset_aislamiento.stack_reset

from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

REPETICIONES = 6


def _p(valores: list[float], q: float) -> float | None:
    if not valores:
        return None
    ordenados = sorted(valores)
    return round(ordenados[min(len(ordenados) - 1, int(q * len(ordenados)))], 3)


async def _llamada_directa(llm_url: str) -> float:
    inicio = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{llm_url}/chat/completions",
            json={"model": "modelo-simulado", "stream": True, "messages": [{"role": "user", "content": "hola"}]},
        )
        assert r.status_code == 200
    return time.perf_counter() - inicio


def _eventos(workspace: Path, task_id: str) -> list[dict]:
    ruta = workspace / ".hermes" / "tiempos" / f"{task_id}.jsonl"
    if not ruta.exists():
        return []
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip()]


async def _medir(stack, instancia, etiqueta: str) -> dict:
    maker, agent_id, workspace = stack["maker"], stack["agent_id"], stack["workspace"]
    headers = await _login(instancia.base)
    filas = []
    for n in range(REPETICIONES):
        inicio = time.perf_counter()
        creada = await _api(
            "POST", instancia.base, "/tasks", headers, json={"agent_id": agent_id, "prompt": f"{etiqueta} {n}"}
        )
        task_id = creada["id"]
        for _ in range(600):
            estado = (await _api("GET", instancia.base, f"/tasks/{task_id}", headers))["status"]
            if estado in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        total = time.perf_counter() - inicio
        assert estado == "completed", (etiqueta, n, estado)
        async with maker() as s:
            tarea = await s.get(Task, task_id)
        eventos = _eventos(workspace, task_id)
        pasos = [e["t"] for e in eventos if e.get("evento") == "paso"]
        fin = next((e["t"] for e in eventos if e.get("evento") == "fin"), None)
        filas.append(
            {
                "total": total,
                "cola": (tarea.started_at - tarea.queued_at).total_seconds(),
                "arranque_runner": (pasos[0] - tarea.started_at.timestamp()) if pasos else None,
                "bucle_modelo": (fin - pasos[0]) if pasos and fin else None,
                "cierre": (tarea.completed_at.timestamp() - fin) if fin else None,
                "pasos": len(pasos),
            }
        )
        await asyncio.sleep(2.5)

    def serie(clave, desde=0):
        return [f[clave] for f in filas[desde:] if f[clave] is not None]

    return {
        "total_p50_s": _p(serie("total"), 0.5),
        "total_p95_s": _p(serie("total"), 0.95),
        "total_sin_primera_p50_s": _p(serie("total", 1), 0.5),
        "cola_p50_s": _p(serie("cola"), 0.5),
        "arranque_runner_p50_s": _p(serie("arranque_runner"), 0.5),
        "arranque_runner_sin_primera_p50_s": _p(serie("arranque_runner", 1), 0.5),
        "bucle_modelo_p50_s": _p(serie("bucle_modelo"), 0.5),
        "cierre_p50_s": _p(serie("cierre"), 0.5),
        "pasos_promedio": statistics.mean(serie("pasos")),
    }


class TestSobrecargaDeOrquestacion:
    async def test_desglose_de_tiempos_y_efecto_del_runner_en_reserva(self, stack_reset):
        directas = [await _llamada_directa(stack_reset["llm_url"]) for _ in range(REPETICIONES)]

        sin_reserva = stack_reset["spawn"]("instancia-sin-reserva")
        await sin_reserva.wait_ready()
        base = await _medir(stack_reset, sin_reserva, "Sin reserva")
        sin_reserva.kill_group()

        con_reserva = stack_reset["spawn"]("instancia-con-reserva", extra_env={"RUNNER_RESERVA_ENABLED": "true"})
        await con_reserva.wait_ready()
        reserva = await _medir(stack_reset, con_reserva, "Con reserva")

        metricas = {"directo_p50_s": _p(directas, 0.5), "sin_reserva": base, "con_reserva": reserva}
        print("METRICAS_ORQUESTACION", json.dumps(metricas))
        assert reserva["arranque_runner_sin_primera_p50_s"] < base["arranque_runner_sin_primera_p50_s"] * 0.6, metricas
        assert reserva["total_sin_primera_p50_s"] < base["total_sin_primera_p50_s"], metricas


class TestDesgloseGuardadoConLaTarea:
    async def test_una_respuesta_lenta_del_modelo_queda_explicada_en_la_tarea(self, stack_reset):
        stack_reset["handler"].delay_lenta = 6.0
        a = stack_reset["spawn"]("instancia-desglose")
        await a.wait_ready()
        headers = await _login(a.base)
        task_id = (
            await _api(
                "POST",
                a.base,
                "/tasks",
                headers,
                json={"agent_id": stack_reset["agent_id"], "prompt": "Pregunta LENTA"},
            )
        )["id"]
        for _ in range(600):
            if (await _api("GET", a.base, f"/tasks/{task_id}", headers))["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.2)
        async with stack_reset["maker"]() as s:
            tarea = await s.get(Task, task_id)
        tiempos = (tarea.metadata_json or {}).get("tiempos")
        print("DESGLOSE_TAREA", json.dumps(tiempos))
        assert tiempos and tiempos["causa_probable"] == "inferencia_lenta", tiempos
        assert tiempos["inferencia_s"] >= 5.5 and tiempos["terminado_por_el_runner"] is True
        assert tiempos["cola_s"] is not None and tiempos["arranque_runner_s"] is not None
