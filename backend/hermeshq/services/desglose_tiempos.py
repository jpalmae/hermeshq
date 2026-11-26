from __future__ import annotations

import json
from pathlib import Path

MAX_EVENTOS = 5000
REPETICIONES_BUCLE = 4
SILENCIO_LARGO_S = 300


def leer_eventos(workspace: Path, task_id: str) -> list[dict]:
    ruta = Path(workspace) / ".hermes" / "tiempos" / f"{task_id}.jsonl"
    try:
        lineas = ruta.read_text(encoding="utf-8").splitlines()[:MAX_EVENTOS]
    except OSError:
        return []
    eventos = []
    for linea in lineas:
        try:
            evento = json.loads(linea)
        except ValueError:
            continue
        if isinstance(evento, dict) and isinstance(evento.get("t"), (int, float)):
            eventos.append(evento)
    return eventos


def _racha_maxima(nombres: list[str]) -> tuple[str, int]:
    mejor, actual, anterior = ("", 0), 0, None
    for nombre in nombres:
        actual = actual + 1 if nombre == anterior else 1
        anterior = nombre
        if actual > mejor[1]:
            mejor = (nombre, actual)
    return mejor


def desglose(eventos: list[dict], iniciado: float | None, terminado: float | None, cola_s: float | None) -> dict:
    pasos = [e["t"] for e in eventos if e.get("evento") == "paso"]
    herramientas = [e for e in eventos if e.get("evento") == "herramienta"]
    fin = next((e["t"] for e in eventos if e.get("evento") == "fin"), None)
    ultimo = max((e["t"] for e in eventos), default=None)
    herramientas_s = round(sum(float(e.get("ms") or 0) for e in herramientas) / 1000, 3)
    por_herramienta: dict[str, list[float]] = {}
    for evento in herramientas:
        por_herramienta.setdefault(str(evento.get("name") or "?"), []).append(float(evento.get("ms") or 0) / 1000)
    lentas = sorted(
        ({"herramienta": n, "llamadas": len(v), "segundos": round(sum(v), 3)} for n, v in por_herramienta.items()),
        key=lambda f: f["segundos"],
        reverse=True,
    )[:3]
    bucle_herramienta, bucle_racha = _racha_maxima([str(e.get("name") or "?") for e in herramientas])
    primeros_textos = [e["t"] for e in eventos if e.get("evento") == "primer_texto"]
    resultado = {
        "cola_s": round(cola_s, 3) if cola_s is not None else None,
        "arranque_runner_s": round(pasos[0] - iniciado, 3) if pasos and iniciado else None,
        "pasos": len(pasos),
        "herramientas_s": herramientas_s,
        "herramientas_mas_lentas": lentas,
        "inferencia_s": round(max(0.0, (fin or terminado or ultimo) - pasos[0] - herramientas_s), 3) if pasos else None,
        "primer_texto_s": round(primeros_textos[0] - pasos[0], 3) if pasos and primeros_textos else None,
        "recortes_de_contexto": sum(1 for e in eventos if e.get("evento") == "recorte"),
        "cierre_s": round(terminado - fin, 3) if fin and terminado else None,
        "terminado_por_el_runner": fin is not None,
        "racha_maxima": {"herramienta": bucle_herramienta, "veces": bucle_racha} if bucle_racha else None,
        "total_s": round(terminado - iniciado, 3) if terminado and iniciado else None,
        "ultimas_llamadas": [
            {k: e.get(k) for k in ("name", "args", "huella")} for e in eventos if e.get("evento") == "llamada"
        ][-10:],
    }
    if fin is None and terminado and ultimo:
        resultado["silencio_final_s"] = round(max(0.0, terminado - ultimo), 3)
    actividad = [e for e in eventos if e.get("evento") == "actividad"]
    if actividad:
        resultado["actividad_final"] = {k: v for k, v in actividad[-1].items() if k not in ("evento", "t")}
        resultado["actividad_final_hace_s"] = round(max(0.0, (terminado or ultimo) - actividad[-1]["t"]), 3)
    estados = [str(e.get("texto") or "")[:160] for e in eventos if e.get("evento") == "estado"]
    if estados:
        resultado["estados"] = estados[-5:]
    generando = [e for e in eventos if e.get("evento") == "generando_herramienta"]
    if generando:
        resultado["ultima_herramienta_generada"] = generando[-1].get("name")
    llamadas = [e for e in eventos if e.get("evento") == "llamada"]
    if llamadas:
        huellas: dict[str, int] = {}
        for e in llamadas:
            clave = f"{e.get('name')}:{e.get('huella')}"
            huellas[clave] = huellas.get(clave, 0) + 1
        resultado["llamadas_identicas_max"] = max(huellas.values())
        resultado["llamadas_registradas"] = len(llamadas)
    resultado["causa_probable"] = clasificar(resultado)
    return resultado


def clasificar(d: dict) -> str:
    if not d.get("pasos"):
        return "runner_bloqueado"
    if (d.get("silencio_final_s") or 0) >= SILENCIO_LARGO_S:
        activo = d.get("actividad_final_hace_s") is not None and d["actividad_final_hace_s"] < SILENCIO_LARGO_S
        return "generacion_larga" if activo or d.get("ultima_herramienta_generada") else "modelo_sin_respuesta"
    if (d.get("racha_maxima") or {}).get("veces", 0) >= REPETICIONES_BUCLE:
        return "bucle"
    total = d.get("total_s") or 0
    if total and d.get("herramientas_s", 0) > 0.5 * total:
        return "herramientas_lentas"
    if total and (d.get("inferencia_s") or 0) > 0.5 * total:
        return "inferencia_lenta"
    return "mixto"


def adjuntar(task, workspace: Path | None) -> None:
    try:
        if workspace is None:
            return
        iniciado = task.started_at.timestamp() if task.started_at else None
        terminado = task.completed_at.timestamp() if task.completed_at else None
        cola = (task.started_at - task.queued_at).total_seconds() if task.started_at and task.queued_at else None
        metadata = dict(task.metadata_json or {})
        metadata["tiempos"] = desglose(leer_eventos(workspace, str(task.id)), iniciado, terminado, cola)
        task.metadata_json = metadata
    except Exception:  # noqa: BLE001
        return
