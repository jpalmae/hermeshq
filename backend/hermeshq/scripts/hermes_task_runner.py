from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from pathlib import Path


def _diag(mensaje: str) -> None:
    print(f"[runner] {mensaje}", file=sys.stderr, flush=True)


_SALIDA = None


def _abrir_salida() -> None:
    """Guarda una copia propia del canal de salida (fd 1) antes de que el agente toque sys.stdout."""
    global _SALIDA
    try:
        _SALIDA = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, closefd=True)
    except OSError:
        _SALIDA = None


_TOPE_SALIDA_DEL_MOTOR = 8000


def _desviar_salida_del_motor() -> None:
    if _SALIDA is None:
        return
    try:
        lectura, escritura = os.pipe()
        os.dup2(escritura, 1)
        os.close(escritura)
    except OSError as exc:
        _diag(f"no se pudo desviar la salida del motor: {exc}")
        return

    def _drenar() -> None:
        restante = _TOPE_SALIDA_DEL_MOTOR
        with os.fdopen(lectura, "r", encoding="utf-8", errors="replace") as canal:
            for linea in canal:
                texto = linea.strip()[:300]
                if texto and restante > 0:
                    restante -= len(texto)
                    _diag(f"motor: {texto}")

    threading.Thread(target=_drenar, name="hq-salida-motor", daemon=True).start()


_ESCRITURA = threading.Lock()


def _escribir(linea: str) -> None:
    destino = _SALIDA if _SALIDA is not None else sys.stdout
    with _ESCRITURA:
        destino.write(linea + "\n")
        destino.flush()


INTERVALO_ACTIVIDAD_S = 30
_ULTIMA_ACTIVIDAD = {"t": 0.0}


def _avisar_actividad(origen: str) -> None:
    ahora = time.time()
    if ahora - _ULTIMA_ACTIVIDAD["t"] < INTERVALO_ACTIVIDAD_S:
        return
    _ULTIMA_ACTIVIDAD["t"] = ahora
    try:
        _emit({"event": "actividad", "origen": origen})
    except Exception:  # noqa: BLE001
        pass


_IDENTIDAD_EJECUCION: dict | None = None


def _iniciar_latido(fd, clave, intervalo) -> None:
    if not _IDENTIDAD_EJECUCION or not isinstance(fd, int) or not clave:
        return
    try:
        espera = max(0.2, float(intervalo or 10))
        os.set_inheritable(fd, False)
    except (OSError, TypeError, ValueError) as exc:
        _diag(f"no se pudo preparar el canal de latido: {exc}")
        return
    identidad = dict(_IDENTIDAD_EJECUCION)
    base = f"{identidad['execution_id']}|{identidad['attempt_id']}|{identidad['lease_generation']}"
    llave = str(clave).encode()

    def _latir() -> None:
        seq = 0
        while True:
            seq += 1
            ts = f"{time.time():.3f}"
            mac = hmac.new(llave, f"{base}|{seq}|{ts}".encode(), hashlib.sha256).hexdigest()
            linea = json.dumps({**identidad, "seq": seq, "ts": ts, "mac": mac}) + "\n"
            try:
                os.write(fd, linea.encode("utf-8"))
            except OSError:
                return
            time.sleep(espera)

    threading.Thread(target=_latir, name="hq-latido", daemon=True).start()


def _emit(payload: dict) -> None:
    if _IDENTIDAD_EJECUCION and payload.get("event") in ("result", "error"):
        payload = {**payload, "execution": dict(_IDENTIDAD_EJECUCION)}
    try:
        _escribir(json.dumps(payload, ensure_ascii=False))
    except (UnicodeEncodeError, ValueError) as exc:
        _diag(f"no se pudo escribir el evento {payload.get('event')} en UTF-8 ({exc}); se reenvía en ASCII")
        _escribir(json.dumps(payload, ensure_ascii=True))


_AVISOS_INTERNOS = (
    re.compile(r"⚠️?\s*Response truncated \(finish_reason='length'\)[^⚠\n]*"),
    re.compile(r"⚠️?\s*Truncated tool call[^\n]*?retrying API call\.\.\."),
    re.compile(r"❌\s*Max retries[^\n]*"),
    re.compile(r"\[Tool loop warning:[^\]]*\]"),
)


_CORTE_GUARDRAIL = re.compile(
    r"I stopped retrying (?P<herramienta>[\w.-]+) because it hit the tool-call guardrail "
    r"\((?P<motivo>[^)]*)\)[^\n]*",
    re.IGNORECASE,
)
_SIGUIENTE_PASO_EN = re.compile(
    r"The last tool result explains the blocker[^\n]*|the next step is to change strategy[^\n.]*\.?",
    re.IGNORECASE,
)
_MENSAJE_CORTE = (
    "No pude terminar esta parte: la herramienta {herramienta} falló varias veces seguidas sin dar un resultado "
    "distinto. Lo que sí quedó listo está arriba; si quieres, lo intento de otra forma."
)
_YA_LISTO_ARRIBA = " Lo que sí quedó listo está arriba; si quieres"


_CORTE_STREAM = re.compile(
    r"\s*⚠\s*Stream stalled mid tool-call \((?P<herramientas>[^)]*)\); the action was not executed\.\s*"
    r"Ask me to retry if you want to continue\.",
    re.IGNORECASE,
)
_MENSAJE_CORTE_STREAM = (
    "\n\n⚠ Se cortó la conexión con el modelo mientras pedía usar {herramientas}; esa acción no se ejecutó. "
    "Pídeme reintentarlo si quieres continuar."
)
_CORTE_STREAM_ES = re.compile(
    r"\s*⚠ Se cortó la conexión con el modelo mientras pedía usar (?P<herramientas>.+?); esa acción no se "
    r"ejecutó\. Pídeme reintentarlo si quieres continuar\."
)
CONTENIDO_MINIMO = 20


def corte_de_stream_sin_contenido(texto: str | None) -> str | None:
    if not texto:
        return None
    for patron in (_CORTE_STREAM, _CORTE_STREAM_ES):
        coincidencia = patron.search(texto)
        if coincidencia is not None:
            if len(patron.sub("", texto).strip()) >= CONTENIDO_MINIMO:
                return None
            return coincidencia.group("herramientas")
    return None


RESPUESTA_VACIA_DEL_MOTOR = "(empty)"
MARCA_RESPUESTA_VACIA = "empty_response_exhausted"
AVISO_SIN_RESPUESTA_FINAL = (
    "⚠️ El modelo no devolvió el mensaje final de esta solicitud (respuesta vacía tras los reintentos). Antes de eso "
    "se ejecutaron estas acciones: {herramientas}. No sé si el resultado quedó completo: revísalo, o escribe "
    "«continúa» para que lo retome."
)


def es_respuesta_vacia_del_motor(texto) -> bool:
    return isinstance(texto, str) and texto.strip() == RESPUESTA_VACIA_DEL_MOTOR


_FILA_DE_TABLA = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M)


def limpiar_aviso_interno(texto: str) -> str:
    texto = _CORTE_STREAM.sub(lambda m: _MENSAJE_CORTE_STREAM.format(herramientas=m.group("herramientas")), texto)
    texto = _CORTE_GUARDRAIL.sub(lambda m: _MENSAJE_CORTE.format(herramienta=m.group("herramienta")), texto)
    texto = _SIGUIENTE_PASO_EN.sub("", texto)
    for patron in _AVISOS_INTERNOS:
        texto = patron.sub("", texto)
    if texto.lstrip().startswith("No pude terminar esta parte"):
        texto = texto.replace(_YA_LISTO_ARRIBA, " Si quieres", 1)
    return _FILA_DE_TABLA.sub(lambda m: m.group(0).replace("\\|", "∣"), texto)


def texto_final(result: dict, emitted_chunks: list) -> str:
    final = result.get("final_response")
    if isinstance(final, str) and final.strip():
        return limpiar_aviso_interno(final).strip()
    crudo = "".join(chunk for chunk in emitted_chunks if isinstance(chunk, str)).strip()
    return limpiar_aviso_interno(crudo).strip()


def _extract_tool_calls(messages: list[dict]) -> list[dict]:
    extracted: list[dict] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls", []) or []:
            extracted.append(
                {
                    "name": tool_call.get("function", {}).get("name", "tool"),
                    "status": "completed",
                    "payload": tool_call,
                }
            )
    return extracted


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "last_prompt_tokens",
    "api_calls",
    "estimated_cost_usd",
    "turn_exit_reason",
)
_USAGE_AGENT_ATTRS = {
    "input_tokens": "session_input_tokens",
    "output_tokens": "session_output_tokens",
    "reasoning_tokens": "session_reasoning_tokens",
    "cache_read_tokens": "session_cache_read_tokens",
    "cache_write_tokens": "session_cache_write_tokens",
    "prompt_tokens": "session_prompt_tokens",
    "completion_tokens": "session_completion_tokens",
    "total_tokens": "session_total_tokens",
    "estimated_cost_usd": "session_estimated_cost_usd",
}


def usage_from_result(result: dict) -> dict:
    return {clave: result.get(clave) for clave in _USAGE_KEYS if result.get(clave) is not None}


def _usage_from_agent(agent) -> dict:
    usage: dict = {}
    for clave, atributo in _USAGE_AGENT_ATTRS.items():
        valor = getattr(agent, atributo, None)
        if valor:
            usage[clave] = valor
    return usage


def build_result_payload(result: dict, final_response: str, messages: list, tool_calls: list) -> dict:
    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    api_calls = result.get("api_calls")
    iterations = int(api_calls) if isinstance(api_calls, int) else len(assistant_messages)
    return {
        "event": "result",
        "final_response": final_response,
        "messages": messages,
        "tool_calls": tool_calls,
        "tokens_used": max(256, len(str(result).split())),
        "iterations": iterations,
        "usage": usage_from_result(result),
        "engine": "hermes-agent",
        **({"exposicion": dict(_EXPOSICION)} if _EXPOSICION else {}),
        **({"compactacion": dict(_COMPACTACION)} if _COMPACTACION else {}),
    }


def _fusionar(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for clave, valor in extra.items():
        if isinstance(valor, dict) and isinstance(merged.get(clave), dict):
            merged[clave] = _fusionar(merged[clave], valor)
        else:
            merged[clave] = valor
    return merged


_RESUMEN_SENAL = "maximum number of tool-calling iterations"


def _es_llamada_resumen(kwargs: dict) -> bool:
    if kwargs.get("tools"):
        return False
    mensajes = kwargs.get("messages") or []
    if not mensajes:
        return False
    ultimo = mensajes[-1]
    if not isinstance(ultimo, dict) or ultimo.get("role") != "user":
        return False
    contenido = ultimo.get("content")
    return isinstance(contenido, str) and _RESUMEN_SENAL in contenido


REPETICIONES_MAXIMAS = 4


def _usa_memoria_persistente(payload: dict) -> bool:
    return bool(payload.get("memoria_persistente", True))


_TEXTO_CIERRE_SIN_HERRAMIENTAS = (
    "No pude terminar esta parte con las herramientas disponibles. Si quieres, lo intento de otra forma."
)


def _cerrar_sin_herramientas(respuesta):
    for eleccion in getattr(respuesta, "choices", None) or []:
        mensaje = getattr(eleccion, "message", None)
        if mensaje is None or not getattr(mensaje, "tool_calls", None):
            continue
        _diag(f"cortacircuito: el modelo pidió {len(mensaje.tool_calls)} herramienta(s) tras el cierre; se descartan")
        mensaje.tool_calls = None
        eleccion.finish_reason = "stop"
        if not (getattr(mensaje, "content", None) or "").strip():
            mensaje.content = _TEXTO_CIERRE_SIN_HERRAMIENTAS
    return respuesta


_AVISO_BUCLE = (
    "Has ejecutado la MISMA llamada a herramienta varias veces seguidas con el mismo resultado y no estás "
    "avanzando. NO vuelvas a llamar ninguna herramienta. Entrega AHORA el resultado final con lo que ya tienes; "
    "si quedó algo sin terminar, dilo en una frase y explica brevemente qué faltó. No repitas la llamada."
)


def _campo(obj, clave):
    if isinstance(obj, dict):
        return obj.get(clave)
    return getattr(obj, clave, None)


RECURSO_ANALIZADO = {"vision_analyze": "image_url", "video_analyze": "video_url"}


def _argumentos_de_firma(nombre, argumentos):
    clave = RECURSO_ANALIZADO.get(nombre)
    if clave is None:
        return argumentos
    try:
        datos = json.loads(argumentos) if isinstance(argumentos, str) else dict(argumentos or {})
    except (TypeError, ValueError):
        return argumentos
    recurso = datos.get(clave) if isinstance(datos, dict) else None
    return ("recurso", recurso) if recurso else argumentos


def _firma_llamada(mensaje) -> tuple | None:
    llamadas = _campo(mensaje, "tool_calls") or []
    firma = []
    for tc in llamadas:
        fn = _campo(tc, "function") or {}
        nombre = _campo(fn, "name")
        firma.append((nombre, _argumentos_de_firma(nombre, _campo(fn, "arguments"))))
    return tuple(firma) if firma else None


def _repeticiones_identicas(mensajes: list) -> int:
    firma = None
    cuenta = 0
    for mensaje in reversed(mensajes):
        rol = _campo(mensaje, "role")
        if rol == "tool":
            continue
        if rol != "assistant":
            break
        actual = _firma_llamada(mensaje)
        if actual is None:
            break
        if firma is None:
            firma, cuenta = actual, 1
        elif actual == firma:
            cuenta += 1
        else:
            break
    return cuenta


VENTANA_CICLO = 12
CICLO_MAXIMO_DISTINTAS = 3


def _ciclo_de_llamadas(mensajes: list) -> bool:
    firmas = []
    for mensaje in reversed(mensajes):
        rol = _campo(mensaje, "role")
        if rol == "tool":
            continue
        if rol != "assistant":
            break
        firma = _firma_llamada(mensaje)
        if firma is None:
            break
        firmas.append(firma)
        if len(firmas) == VENTANA_CICLO:
            return len(set(firmas)) <= CICLO_MAXIMO_DISTINTAS
    return False


_NEGACION_VISION = re.compile(
    r"\b(?:NOT|not (?:in|visible|present|shown|included)|do(?:es)? not (?:contain|show|include|appear)|isn't|aren't|"
    r"no (?:aparecen?|se ven?|est[áa]n?|hay|contiene|muestra|figuran?))\b",
    re.IGNORECASE,
)
_ENTRE_COMILLAS = re.compile(r"[\"“«]([^\"”»\n]{2,60})[\"”»]")
RUTA_NEGADOS_VISION = Path(".hermes") / "vision_negados.json"


def negados_por_vision(mensajes: list | None) -> list[str]:
    nombres = {}
    for m in mensajes or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                funcion = llamada.get("function") if isinstance(llamada, dict) else None
                if isinstance(funcion, dict):
                    nombres[llamada.get("id")] = funcion.get("name")
    negados: list[str] = []
    for m in mensajes or []:
        if not (
            isinstance(m, dict) and m.get("role") == "tool" and nombres.get(m.get("tool_call_id")) == "vision_analyze"
        ):
            continue
        contenido = str(m.get("content") or "")
        try:
            contenido = str(json.loads(contenido).get("analysis") or contenido)
        except (ValueError, AttributeError):
            pass
        for oracion in re.split(r"(?<=[.!?])\s+|\n+", contenido.replace("\\n", "\n")):
            if _NEGACION_VISION.search(oracion):
                for item in _ENTRE_COMILLAS.findall(oracion.replace('\\"', '"')):
                    item = item.strip(" .,:;")
                    if len(item) >= 2 and item not in negados:
                        negados.append(item)
    return negados


def _registrar_negados_vision(mensajes) -> None:
    negados = negados_por_vision(mensajes)
    if not negados:
        return
    try:
        tarea = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}").get("task_id")
        RUTA_NEGADOS_VISION.parent.mkdir(parents=True, exist_ok=True)
        RUTA_NEGADOS_VISION.write_text(
            json.dumps({"task_id": tarea, "negados": negados}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo registrar lo negado por la visión: {exc}")


_PALABRAS_EN = frozenset(
    [
        "i",
        "i'll",
        "i'm",
        "let",
        "me",
        "the",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "will",
        "would",
        "should",
        "could",
        "have",
        "has",
        "had",
        "be",
        "been",
        "and",
        "or",
        "of",
        "to",
        "for",
        "with",
        "from",
        "it",
        "its",
        "it's",
        "we",
        "you",
        "they",
        "he",
        "she",
        "what",
        "which",
        "who",
        "when",
        "where",
        "now",
        "first",
        "then",
        "start",
        "analyze",
        "check",
        "look",
        "loading",
        "load",
        "file",
        "data",
        "need",
        "going",
    ]
)
_PALABRAS_ES = frozenset(
    [
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "de",
        "del",
        "que",
        "y",
        "en",
        "por",
        "para",
        "con",
        "se",
        "es",
        "son",
        "fue",
        "no",
        "lo",
        "le",
        "su",
        "sus",
        "al",
        "como",
        "más",
        "pero",
        "este",
        "esta",
        "estos",
        "estas",
        "hay",
        "ya",
        "muy",
        "también",
        "entre",
        "sobre",
    ]
)


def _es_ingles(parrafo: str) -> bool:
    palabras = re.findall(r"[a-záéíóúñü']+", parrafo.lower())
    if len(palabras) < 4:
        return False
    en = sum(p in _PALABRAS_EN for p in palabras)
    es = sum(p in _PALABRAS_ES for p in palabras)
    return en >= max(3, len(palabras) * 0.25) and es <= len(palabras) * 0.08


def sin_parrafos_en_ingles(texto: str) -> str:
    if not texto:
        return texto
    parrafos = re.split(r"\n\s*\n", texto)
    inicio, fin = 0, len(parrafos)
    while (
        inicio < fin and _es_ingles(parrafos[inicio]) and not parrafos[inicio].lstrip().startswith(("|", "#", "MEDIA:"))
    ):
        inicio += 1
    while (
        fin > inicio
        and _es_ingles(parrafos[fin - 1])
        and not parrafos[fin - 1].lstrip().startswith(("|", "#", "MEDIA:"))
    ):
        fin -= 1
    if inicio == fin or (inicio == 0 and fin == len(parrafos)):
        return texto
    restantes = parrafos[inicio:fin]
    if not any(not _es_ingles(p) for p in restantes):
        return texto
    return "\n\n".join(restantes).strip()


_SIN_MENCIONAR_REVISION = (
    " Esta nota es del sistema y el usuario no la ve: responde a su pedido como si fuera tu primera respuesta, sin "
    "disculparte, sin decir «tienes razón» y sin mencionar esta revisión."
)
_NOTA_IDIOMA = (
    "[Nota del sistema] Tu respuesta anterior quedó en inglés. El usuario escribe en español: entrega la misma "
    "respuesta completa en español, sin volver a llamar herramientas salvo que falte algo." + _SIN_MENCIONAR_REVISION
)
_DISCULPA = re.compile(
    r"\b(disculpa\w*|perd[óo]n\w*|lo siento|mis disculpas|ten[ií]as raz[óo]n|tienes raz[óo]n)\b", re.IGNORECASE
)
_MOTIVO_DE_DISCULPA = re.compile(
    r"no lo hab[ií]a|antes de (responder|contestar)|no (hab[ií]a|lo) (buscado|revisado|verificado|consultado)|"
    r"respuesta anterior|me equivoqu|mi error|deb[ií] (buscar|revisar|consultar|verificar)|"
    r"sin (buscar|revisar|consultar|verificar)",
    re.IGNORECASE,
)


def _solo_disculpa(oracion: str) -> bool:
    propia = _CITADO.sub(" ", oracion)
    if len(propia) > 140 or re.search(r"\*\*|\d|https?://", propia):
        return False
    return bool(_DISCULPA.search(propia) and _MOTIVO_DE_DISCULPA.search(propia))


def quitar_disculpa_por_revision(texto: str) -> str:
    if not texto or not _DISCULPA.search(texto):
        return texto
    lineas = []
    cambio = False
    for linea in texto.split("\n"):
        if linea.lstrip().startswith((">", "|")):
            lineas.append(linea)
            continue
        oraciones = re.split(r"(?<=[.!?…])[ \t]+", linea)
        quedan = [o for o in oraciones if not _solo_disculpa(o)]
        if len(quedan) == len(oraciones):
            lineas.append(linea)
            continue
        cambio = True
        if quedan:
            lineas.append(" ".join(quedan))
    if not cambio:
        return texto
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def respuesta_en_ingles(texto: str, mensaje: str = "") -> bool:
    parrafos = [
        p for p in re.split(r"\n\s*\n", texto or "") if p.strip() and not p.lstrip().startswith(("|", "MEDIA:"))
    ]
    total = sum(len(p) for p in parrafos)
    if total < 80:
        return False
    en_ingles = sum(len(p) for p in parrafos if _es_ingles(p))
    if en_ingles < total * 0.6:
        return False
    palabras = re.findall(r"[a-záéíóúñü']+", (mensaje or "").lower())
    return sum(w in _PALABRAS_ES for w in palabras) >= 2 or not palabras


def correcciones_del_usuario(task_id: str | None) -> list[str]:
    if not task_id:
        return []
    try:
        datos = json.loads((Path(".hermes") / f"correcciones-{task_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        str(c.get("texto") or "").strip() for c in datos if isinstance(c, dict) and str(c.get("texto") or "").strip()
    ]


def mensaje_de_correcciones(correcciones: list[str]) -> dict:
    lineas = "\n".join(f"- «{c}»" for c in correcciones)
    return {
        "role": "user",
        "content": (
            "[CORRECCIONES DEL USUARIO MIENTRAS TRABAJAS: mandan sobre el plan y sobre lo que llevas hecho.]\n"
            f"{lineas}\n"
            "Aplícalas ahora: ajusta los criterios, vuelve a calcular lo que dependa de ellas y menciónalas en el "
            "próximo avance y en la entrega."
        ),
    }


_PREFIJO_PLATAFORMA = re.compile(r"^\s*\[[^\]]*\]\s*")
_CITA_TEAMS = re.compile(r"<quoted\s+messageId\s*=\s*\"\d+\"\s*/?>", re.I)


def _texto_propio_del_usuario(mensaje: str | None) -> str:
    texto = _CITA_TEAMS.sub("", mensaje or "")
    anterior = None
    while anterior != texto:
        anterior, texto = texto, _PREFIJO_PLATAFORMA.sub("", texto, count=1)
    if texto.lstrip().lower().startswith("conversación reciente:"):
        texto = texto.rsplit("Último mensaje del usuario:", 1)[-1]
    return texto.strip()


def _aplicar_overrides_resumen(request_overrides: dict | None, adaptativo: bool) -> None:
    try:
        from openai.resources.chat.completions import Completions

        original = Completions.create
        overrides = dict(request_overrides or {})

        def create(self, *args, **kwargs):
            forzar_cierre = False
            _avisar_actividad("modelo")
            if _REGISTRO.get("tiempos") is not None:
                try:
                    _REGISTRO["tiempos"].llamadas(kwargs.get("messages"))
                except Exception:  # noqa: BLE001
                    pass
            _registrar_negados_vision(kwargs.get("messages"))
            try:
                tarea_actual = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}").get("task_id")
                correcciones = correcciones_del_usuario(tarea_actual)
                if correcciones and kwargs.get("messages"):
                    kwargs["messages"] = list(kwargs["messages"]) + [mensaje_de_correcciones(correcciones)]
                    _REGISTRO["correcciones"] = len(correcciones)
            except Exception as exc:  # noqa: BLE001
                _diag(f"no se pudieron leer las correcciones del usuario: {exc}")
            if kwargs.get("tools"):
                repeticiones = _repeticiones_identicas(kwargs.get("messages") or [])
                ciclo = _ciclo_de_llamadas(kwargs.get("messages") or [])
                if repeticiones >= REPETICIONES_MAXIMAS or ciclo:
                    _diag(
                        f"cortacircuito: {repeticiones} llamadas idénticas seguidas"
                        + (f" y un ciclo de {VENTANA_CICLO} llamadas con pocas variantes" if ciclo else "")
                        + "; forzando cierre"
                    )
                    kwargs.pop("tools", None)
                    kwargs.pop("tool_choice", None)
                    mensajes = list(kwargs.get("messages") or [])
                    mensajes.append({"role": "user", "content": _AVISO_BUCLE})
                    kwargs["messages"] = mensajes
                    forzar_cierre = True
            if forzar_cierre or _es_llamada_resumen(kwargs):
                extra = dict(kwargs.get("extra_body") or {})
                for clave, valor in overrides.items():
                    if clave == "extra_body" and isinstance(valor, dict):
                        extra = _fusionar(extra, valor)
                    else:
                        kwargs[clave] = valor
                if adaptativo:
                    plantilla = dict(extra.get("chat_template_kwargs") or {})
                    plantilla["enable_thinking"] = False
                    extra["chat_template_kwargs"] = plantilla
                if extra:
                    kwargs["extra_body"] = extra
            respuesta = original(self, *args, **kwargs)
            return _cerrar_sin_herramientas(respuesta) if forzar_cierre else respuesta

        Completions.create = create
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo aplicar el cortacircuito/overrides: {exc}")


def _glob_sin_mayusculas(nombre: str) -> str:
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in nombre)


def _activar_busqueda_de_archivos() -> None:
    try:
        from tools.file_operations import ShellFileOperations
    except Exception:  # noqa: BLE001
        return
    if getattr(ShellFileOperations, "_hq_busqueda_corregida", False):
        return
    original_search = ShellFileOperations.search
    original_files = ShellFileOperations._search_files
    ShellFileOperations._hq_originales = (original_search, original_files)

    def search(self, pattern, path=".", target="content", file_glob=None, **kwargs):
        if target == "files" and file_glob and str(pattern or "").strip() in ("", ".*", "*", ".+", "**"):
            pattern = file_glob
        return original_search(self, pattern=pattern, path=path, target=target, file_glob=file_glob, **kwargs)

    def _search_files(self, pattern, path, limit, offset):
        texto = str(pattern or "")
        if not self._has_command("rg") and "/" not in texto and not any(c in texto for c in "*?["):
            pattern = f"*{_glob_sin_mayusculas(texto)}*"
        return original_files(self, pattern, path, limit, offset)

    ShellFileOperations.search = search
    ShellFileOperations._search_files = _search_files
    ShellFileOperations._hq_busqueda_corregida = True


def _activar_guardia(payload: dict) -> None:
    if not payload.get("workspace_guard", True):
        return
    try:
        import importlib.util

        carpeta = Path(__file__).resolve().parent / "guard_site"
        spec = importlib.util.spec_from_file_location("hermeshq_workspace_guard", carpeta / "workspace_guard.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        os.environ["HERMES_SESSION_CWD"] = os.environ.get("HERMES_SESSION_CWD") or payload["cwd"]
        modulo.install(payload["cwd"])
        previo = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(carpeta) + (os.pathsep + previo if previo else "")
    except Exception as exc:  # noqa: BLE001  # a failure to enable the guard must be visible but must not stop the task
        _diag(f"no se pudo activar la guardia de carpetas: {exc}")


SOLO_DEL_RUNNER = (
    "HERMESHQ_AGENT_TOKEN",
    "HERMESHQ_ATTEMPT_ID",
    "HERMESHQ_LEASE_GENERATION",
    "HERMESHQ_EXECUTION_ID",
    "HERMESHQ_TASK_PAYLOAD",
)

_EXPOSICION: dict = {}


def _cargar_exposicion():
    import importlib.util

    ruta = Path(__file__).resolve().parent / "exposicion_herramientas.py"
    spec = importlib.util.spec_from_file_location("hermeshq_exposicion_herramientas", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _preparar_exposicion(payload: dict) -> None:
    config = payload.get("exposicion")
    if not isinstance(config, dict):
        return
    try:
        ex = _cargar_exposicion()
        from openai.resources.chat.completions import Completions
        from tools.registry import registry

        estado = {"activadas": set(), "catalogo": [], "toolset_de": {}, "ex": ex}
        estado["usadas"] = ex.usadas_en_historial(payload.get("conversation_history"))
        estado["toolsets_pedido"] = config.get("toolsets_pedido")
        estado["herramientas_pedido"] = list(config.get("herramientas_pedido") or [])
        _EXPOSICION_ESTADO["estado"] = estado

        def _meta(args, **_kw):
            return ex.responder_meta(
                args or {}, estado["catalogo"], estado["activadas"], toolset_de=estado["toolset_de"]
            )

        registry.register(
            name=ex.META,
            toolset="hermeshq_exposicion",
            schema=ex.ESQUEMA_META["function"],
            handler=_meta,
            description="Buscar y activar herramientas",
        )
        toolsets = list(payload.get("enabled_toolsets") or [])
        if toolsets and "hermeshq_exposicion" not in toolsets:
            payload["enabled_toolsets"] = toolsets + ["hermeshq_exposicion"]
        original = Completions.create

        def create(self, *args, **kwargs):
            herramientas = kwargs.get("tools")
            if herramientas:
                if not estado["catalogo"]:
                    estado["catalogo"] = [h for h in herramientas if ex.nombre(h) != ex.META]
                    estado["toolset_de"] = {
                        ex.nombre(h): registry.get_toolset_for_tool(ex.nombre(h)) or "" for h in herramientas
                    }
                visibles = ex.expuestas(
                    estado["catalogo"],
                    toolset_de=estado["toolset_de"],
                    toolsets_pedido=estado["toolsets_pedido"],
                    herramientas_pedido=estado["herramientas_pedido"],
                    usadas=estado["usadas"] | ex.usadas_en_historial(kwargs.get("messages")),
                    activadas=estado["activadas"],
                )
                kwargs["tools"] = ex.filtrar(herramientas, visibles)
                _EXPOSICION.update(
                    {
                        "expuestas": len(kwargs["tools"]),
                        "catalogo": len(estado["catalogo"]),
                        "activadas": sorted(estado["activadas"]),
                    }
                )
            return original(self, *args, **kwargs)

        Completions.create = create
        _diag(f"exposición progresiva activa (pedido: {estado['toolsets_pedido']})")
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo activar la exposición progresiva: {exc}")


_ACTIVADAS_BAJO_DEMANDA: list[str] = []
_DURACION_HERRAMIENTA_MS: dict[str, int] = {}
_CLASIFICADOR = []


def _clasificar_resultado(nombre: str, resultado) -> str:
    try:
        import importlib.util

        if not _CLASIFICADOR:
            ruta = Path(__file__).resolve().parent.parent / "services" / "clasificacion_herramientas.py"
            spec = importlib.util.spec_from_file_location("hermeshq_clasificacion_herramientas", ruta)
            modulo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modulo)
            _CLASIFICADOR.append(modulo)
        return _CLASIFICADOR[0].clasificar(nombre, resultado)
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo clasificar el resultado de {nombre}: {exc}")
        return "sin_clasificar"


def _herramienta_terminada(tool_call_id, nombre, args, resultado) -> None:
    evento = {
        "event": "tool_finished",
        "name": str(nombre or "tool"),
        "clase": _clasificar_resultado(str(nombre or ""), resultado),
        "ms": _DURACION_HERRAMIENTA_MS.pop(str(nombre or "tool"), 0),
    }
    _emit(evento)


def _activar_integraciones_bajo_demanda(payload: dict) -> None:
    diferidos = [str(t) for t in (payload.get("toolsets_diferidos") or []) if t]
    if not diferidos:
        return
    try:
        from run_agent import AIAgent
        from tools.registry import registry

        ex = _cargar_exposicion()
        original = AIAgent._repair_tool_call

        def reparar(self, tool_name):
            try:
                activada = ex.activar_diferida(
                    self, tool_name, diferidos, registry, (_EXPOSICION_ESTADO.get("estado") or None)
                )
            except Exception as exc:  # noqa: BLE001
                _diag(f"no se pudo activar {tool_name} bajo demanda: {exc}")
                activada = None
            if activada:
                _ACTIVADAS_BAJO_DEMANDA.append(activada)
                _diag(f"herramienta {activada} activada bajo demanda (el router la había quitado)")
                _emit({"event": "tool_activated", "name": activada})
                return activada
            return original(self, tool_name)

        AIAgent._repair_tool_call = reparar
        _diag(f"activación bajo demanda lista para: {', '.join(diferidos)}")
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo preparar la activación bajo demanda: {exc}")


MARGEN_COMPACTACION = 0.10
COLA_MINIMA = 4000


def calibrar_compactacion(compresor, fijo_tokens: int) -> int | None:
    umbral = int(getattr(compresor, "threshold_tokens", 0) or 0)
    cola = int(getattr(compresor, "tail_token_budget", 0) or 0)
    if not umbral or not cola:
        return None
    resumen = int(getattr(compresor, "max_summary_tokens", 0) or 0)
    disponible = umbral - int(fijo_tokens) - resumen - int(umbral * MARGEN_COMPACTACION)
    nueva = max(COLA_MINIMA, min(cola, disponible))
    compresor.tail_token_budget = nueva
    return nueva


def _fijo_estimado(agent, payload: dict) -> int:
    herramientas = list(getattr(agent, "tools", None) or [])
    estado = _EXPOSICION_ESTADO.get("estado")
    if estado is not None:
        from tools.registry import registry

        ex = estado["ex"]
        visibles = ex.expuestas(
            herramientas,
            toolset_de={ex.nombre(h): registry.get_toolset_for_tool(ex.nombre(h)) or "" for h in herramientas},
            toolsets_pedido=estado["toolsets_pedido"],
            herramientas_pedido=estado["herramientas_pedido"],
            usadas=estado["usadas"],
        )
        herramientas = ex.filtrar(herramientas, visibles)
    sistema = len(str(payload.get("system_prompt") or "")) + 12000
    return (len(json.dumps(herramientas, ensure_ascii=False)) + sistema) // 4


_EXPOSICION_ESTADO: dict = {}
_COMPACTACION: dict = {}
_REGISTRO: dict = {}
_HERRAMIENTAS_INICIADAS: list[str] = []


def _activar_pensamiento_adaptativo() -> None:
    """Thinking stays on for planning calls and is switched off for the calls that only digest tool results."""
    try:
        from openai.resources.chat.completions import Completions

        original = Completions.create

        def create(self, *args, **kwargs):
            mensajes = kwargs.get("messages") or []
            if mensajes and isinstance(mensajes[-1], dict) and mensajes[-1].get("role") == "tool":
                extra = dict(kwargs.get("extra_body") or {})
                plantilla = dict(extra.get("chat_template_kwargs") or {})
                plantilla["enable_thinking"] = False
                extra["chat_template_kwargs"] = plantilla
                kwargs["extra_body"] = extra
            return original(self, *args, **kwargs)

        Completions.create = create
    except Exception as exc:  # noqa: BLE001  # without the patch the run simply keeps thinking on
        _diag(f"no se pudo activar el pensamiento adaptativo: {exc}")


RESULTADOS_RECIENTES = 2
LIMITE_RESULTADO_RECIENTE = 16000
LIMITE_RESULTADO_ANTIGUO = 6000
HERRAMIENTAS_SIN_RECORTE = frozenset({"skill_view"})


AVISO_SKILL_REPETIDA = (
    "[La skill «{skill}» ya se cargó antes en esta tarea y sus instrucciones siguen en esta conversación. "
    "No la vuelvas a cargar: usa las instrucciones que ya tienes.]"
)


def _nombre_skill(argumentos) -> str:
    try:
        datos = json.loads(argumentos) if isinstance(argumentos, str) else dict(argumentos or {})
    except (TypeError, ValueError):
        return ""
    return str(datos.get("name") or "").strip()


def recortar_resultados(mensajes: list) -> tuple[list, int]:
    nombres = {}
    habilidades = {}
    for mensaje in mensajes:
        if isinstance(mensaje, dict):
            for llamada in mensaje.get("tool_calls") or []:
                if isinstance(llamada, dict) and llamada.get("id"):
                    funcion = llamada.get("function") or {}
                    nombres[llamada["id"]] = str(funcion.get("name") or "")
                    if funcion.get("name") == "skill_view":
                        habilidades[llamada["id"]] = _nombre_skill(funcion.get("arguments"))
    indices = [i for i, m in enumerate(mensajes) if isinstance(m, dict) and m.get("role") == "tool"]
    recientes = set(indices[-RESULTADOS_RECIENTES:])
    nuevos, recortados = [], 0
    vistas = set()
    for i, mensaje in enumerate(mensajes):
        contenido = mensaje.get("content") if isinstance(mensaje, dict) else None
        if i not in indices or not isinstance(contenido, str):
            nuevos.append(mensaje)
            continue
        nombre = str(mensaje.get("name") or nombres.get(mensaje.get("tool_call_id"), ""))
        skill = habilidades.get(mensaje.get("tool_call_id")) if nombre == "skill_view" else None
        if skill:
            if skill in vistas:
                nuevos.append({**mensaje, "content": AVISO_SKILL_REPETIDA.format(skill=skill)})
                recortados += 1
                continue
            vistas.add(skill)
        limite = LIMITE_RESULTADO_RECIENTE if i in recientes else LIMITE_RESULTADO_ANTIGUO
        if nombre in HERRAMIENTAS_SIN_RECORTE or len(contenido) <= limite:
            nuevos.append(mensaje)
            continue
        omitidos = len(contenido) - limite
        nuevos.append(
            {
                **mensaje,
                "content": contenido[:limite]
                + f"\n[... {omitidos} caracteres omitidos para ahorrar contexto; si lo necesitas, vuelve a leerlo]",
            }
        )
        recortados += 1
    return nuevos, recortados


def _activar_recorte_contexto(registro) -> None:
    try:
        from openai.resources.chat.completions import Completions

        original = Completions.create

        def create(self, *args, **kwargs):
            mensajes = kwargs.get("messages")
            if isinstance(mensajes, list):
                kwargs["messages"], recortados = recortar_resultados(mensajes)
                registro.recorte(recortados, len(mensajes))
            return original(self, *args, **kwargs)

        Completions.create = create
    except Exception as exc:  # noqa: BLE001
        _diag(f"no se pudo activar el recorte de contexto: {exc}")


class RegistroTiempos:
    def __init__(self, cwd: str, task_id: str) -> None:
        self.ruta = None
        self.paso = 0
        self._esperando_texto = False
        try:
            carpeta = Path(cwd) / ".hermes" / "tiempos"
            carpeta.mkdir(parents=True, exist_ok=True)
            self.ruta = carpeta / f"{task_id}.jsonl"
        except OSError as exc:
            _diag(f"no se pudo preparar el registro de tiempos: {exc}")

    def _escribir(self, evento: dict) -> None:
        if self.ruta is None:
            return
        try:
            with self.ruta.open("a", encoding="utf-8") as archivo:
                archivo.write(json.dumps({**evento, "t": round(time.time(), 3)}, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            _diag(f"no se pudo escribir el registro de tiempos: {exc}")

    def paso_iniciado(self, n: int) -> None:
        self.paso = n
        self._esperando_texto = True
        self._escribir({"evento": "paso", "n": n})

    def texto_visible(self) -> None:
        if self._esperando_texto:
            self._esperando_texto = False
            self._escribir({"evento": "primer_texto", "paso": self.paso})

    def recorte(self, recortados: int, mensajes: int) -> None:
        self._escribir({"evento": "recorte", "paso": self.paso, "recortados": recortados, "mensajes": mensajes})

    def herramienta(self, nombre: str, ms: int, ok: bool) -> None:
        self._escribir({"evento": "herramienta", "paso": self.paso, "name": nombre, "ms": ms, "ok": ok})

    def llamadas(self, mensajes) -> None:
        if not isinstance(mensajes, list):
            return
        vistas = getattr(self, "_llamadas_vistas", set())
        for mensaje in mensajes:
            for llamada in (_campo(mensaje, "tool_calls") or []) if _campo(mensaje, "role") == "assistant" else []:
                ident = str(_campo(llamada, "id") or "")
                if not ident or ident in vistas:
                    continue
                vistas.add(ident)
                fn = _campo(llamada, "function") or {}
                argumentos = str(_campo(fn, "arguments") or "")
                self._escribir(
                    {
                        "evento": "llamada",
                        "paso": self.paso,
                        "name": str(_campo(fn, "name") or "?"),
                        "args": argumentos[:300],
                        "huella": hashlib.sha1(argumentos.encode("utf-8", "replace")).hexdigest()[:12],
                    }
                )
        self._llamadas_vistas = vistas

    def actividad(self, tipo: str, cantidad: int) -> None:
        contadores = self.__dict__.setdefault("_actividad", {})
        contadores[tipo] = contadores.get(tipo, 0) + int(cantidad or 0)
        ahora = time.time()
        if ahora - self.__dict__.get("_actividad_escrita", 0.0) >= 15:
            self._actividad_escrita = ahora
            self._escribir({"evento": "actividad", "paso": self.paso, **contadores})

    def generando_herramienta(self, nombre) -> None:
        self._escribir({"evento": "generando_herramienta", "paso": self.paso, "name": str(nombre or "?")[:80]})

    def estado(self, tipo, mensaje="") -> None:
        self._escribir({"evento": "estado", "paso": self.paso, "tipo": str(tipo)[:20], "texto": str(mensaje)[:200]})

    def fin(self) -> None:
        self._escribir({"evento": "fin", "paso": self.paso})


def _mensaje_del_usuario(payload: dict) -> str:
    original = (payload.get("metadata") or {}).get("mensaje_usuario")
    if isinstance(original, str) and original.strip():
        return original[:600]
    return str(payload.get("prompt") or "")[:600]


_DIRECTIVA_FORMATO_PROMPT = re.compile(r"^\s*\[FORMATO PEDIDO EN ESTE MENSAJE: ([^.\]]+)")


def _formato_pedido(payload: dict) -> str:
    coincidencia = _DIRECTIVA_FORMATO_PROMPT.match(str(payload.get("prompt") or ""))
    nombre = coincidencia.group(1).strip().lower() if coincidencia else ""
    for clave in ("excel", "html", "word", "pdf"):
        if nombre.startswith(clave):
            return clave
    return "chat" if nombre.startswith("respuesta en el chat") else ""


def _mensaje_para_herramientas(payload: dict) -> str:
    original = (payload.get("metadata") or {}).get("mensaje_usuario")
    if isinstance(original, str) and original.strip():
        return original[:4000]
    return str(payload.get("prompt") or "")[:4000]


def _ultima_respuesta(historial) -> str:
    for mensaje in reversed(historial or []):
        if not isinstance(mensaje, dict) or mensaje.get("role") != "assistant":
            continue
        contenido = mensaje.get("content")
        if isinstance(contenido, list):
            contenido = " ".join(str(p.get("text") or "") for p in contenido if isinstance(p, dict))
        if isinstance(contenido, str) and contenido.strip() and not contenido.lstrip().startswith(_TURNO_CORTADO):
            return contenido
    return ""


def _pedidos_previos_del_usuario(historial, maximo: int = 8, limite: int = 6000) -> list[str]:
    textos: list[str] = []
    for mensaje in historial or []:
        if not isinstance(mensaje, dict) or mensaje.get("role") != "user":
            continue
        contenido = mensaje.get("content")
        if isinstance(contenido, list):
            contenido = " ".join(str(p.get("text") or "") for p in contenido if isinstance(p, dict))
        if isinstance(contenido, str) and contenido.strip() and contenido.strip() != "/reset":
            textos.append(contenido)
    salida: list[str] = []
    total = 0
    for texto in reversed(textos[-maximo:]):
        if total + len(texto) > limite:
            texto = texto[: max(0, limite - total)]
        if not texto:
            break
        salida.append(texto)
        total += len(texto)
    return list(reversed(salida))


_TURNO_CORTADO = "(Turno anterior sin completar"


def _ultimo_pedido_del_usuario(historial) -> str:
    for mensaje in reversed(historial or []):
        if not isinstance(mensaje, dict) or mensaje.get("role") != "user":
            continue
        contenido = mensaje.get("content")
        if isinstance(contenido, list):
            contenido = " ".join(str(p.get("text") or "") for p in contenido if isinstance(p, dict))
        if isinstance(contenido, str) and contenido.strip() and contenido.strip() != "/reset":
            return contenido
    return ""


def _cargar_plugin_correo(hermes_home: str):
    ruta = Path(hermes_home) / "plugins" / "hermeshq_ms365_mail" / "__init__.py"
    if not ruta.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermeshq_ms365_mail_envio_directo", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _texto_envio(envio: dict) -> str:
    preview = envio.get("preview") or {}

    def _lista(*claves):
        salida = []
        for clave in claves:
            valor = preview.get(clave)
            salida += [valor] if isinstance(valor, str) else list(valor or [])
        return [d for d in salida if d]

    reenvio = envio.get("herramienta") == "ms365_mail_forward"
    lineas = [f"✅ Correo {'reenviado' if reenvio else 'enviado'} (confirmado por el servidor)."]
    for etiqueta, claves in (("Para", ("para", "responde_a", "tambien_a")), ("CC", ("cc",)), ("CCO", ("cco",))):
        destinos = _lista(*claves)
        if destinos:
            lineas.append(f"**{etiqueta}:** {', '.join(destinos)}")
    if preview.get("asunto"):
        lineas.append(f"**Asunto:** {preview['asunto']}")
    return "\n".join(lineas)


def _chat_con_usuario(payload: dict) -> bool:
    metadata = payload.get("metadata") or {}
    return (
        metadata.get("source") == "public_chat" and not metadata.get("scheduled") and not metadata.get("background_job")
    )


def _borrador_escrito_anterior(historial) -> str:
    respuestas: list[str] = []
    for mensaje in reversed(historial or []):
        if not isinstance(mensaje, dict) or mensaje.get("role") != "assistant":
            continue
        contenido = mensaje.get("content")
        if isinstance(contenido, list):
            contenido = " ".join(str(p.get("text") or "") for p in contenido if isinstance(p, dict))
        if isinstance(contenido, str) and contenido.strip():
            respuestas.append(contenido)
        if len(respuestas) == 2:
            break
    if not respuestas:
        return ""
    if "NO se envió" in respuestas[0]:
        return respuestas[1] if len(respuestas) > 1 else ""
    return respuestas[0]


_PREGUNTA_ENVIO = re.compile(r"¿[^?]{0,40}env[ií]", re.I)
_HERRAMIENTAS_CORREO = {"ms365_mail_send", "ms365_mail_reply", "ms365_mail_forward"}


def _mensajes_herramienta(herramienta: str, args: dict, resultado: dict, llamada_id: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": llamada_id,
                    "type": "function",
                    "function": {"name": herramienta, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        },
        {
            "role": "tool",
            "name": herramienta,
            "tool_call_id": llamada_id,
            "content": json.dumps(resultado, ensure_ascii=False),
        },
    ]


def _borrador_a_vista_previa(payload: dict, final_response: str, messages: list) -> tuple[str, list] | None:
    if not _chat_con_usuario(payload) or not final_response or not _PREGUNTA_ENVIO.search(final_response):
        return None
    ultimo_usuario = max(
        (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"), default=-1
    )
    for mensaje in messages[ultimo_usuario + 1 :]:
        for llamada in (mensaje.get("tool_calls") or []) if isinstance(mensaje, dict) else []:
            if ((llamada.get("function") or {}).get("name") or "") in _HERRAMIENTAS_CORREO:
                return None
    try:
        plugin = _cargar_plugin_correo(payload["hermes_home"])
        vista = plugin.vista_previa_desde_borrador_escrito(final_response) if plugin else None
    except Exception as exc:  # noqa: BLE001
        _diag(f"borrador escrito sin vista previa: {exc}")
        return None
    if not vista:
        return None
    texto = vista["resultado"]["borrador"] + "\n\n¿Lo envío?"
    extra = _mensajes_herramienta(
        vista["herramienta"], vista["args"], vista["resultado"], f"vista-previa-{payload.get('task_id')}"
    )
    return texto, list(messages) + extra + [{"role": "assistant", "content": texto}]


def _envio_aprobado_directo(payload: dict) -> dict | None:
    if not _chat_con_usuario(payload):
        return None
    try:
        plugin = _cargar_plugin_correo(payload["hermes_home"])
        envio = plugin.ejecutar_aprobacion_pendiente() if plugin else None
        if plugin and not envio and plugin.es_aprobacion_pura():
            escrito = _borrador_escrito_anterior(payload.get("conversation_history"))
            if escrito and plugin.vista_previa_desde_borrador_escrito(escrito, ya_mostrado=True):
                envio = plugin.ejecutar_aprobacion_pendiente()
    except Exception as exc:  # noqa: BLE001
        _diag(f"envío directo no disponible: {exc}")
        return None
    resultado = (envio or {}).get("resultado") or {}
    if not envio or not (
        resultado.get("success") or resultado.get("ya_enviado") or resultado.get("resultado_incierto")
    ):
        return None
    llamada_id = f"envio-directo-{payload.get('task_id')}"
    texto = _texto_envio(envio) if resultado.get("success") else str(resultado.get("error") or "")
    mensajes = [
        {"role": "user", "content": payload.get("prompt") or ""},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": llamada_id,
                    "type": "function",
                    "function": {
                        "name": envio["herramienta"],
                        "arguments": json.dumps(envio["args"], ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": envio["herramienta"],
            "tool_call_id": llamada_id,
            "content": json.dumps(envio["resultado"], ensure_ascii=False),
        },
        {"role": "assistant", "content": texto},
    ]
    return build_result_payload({"api_calls": 0}, texto, mensajes, _extract_tool_calls(mensajes))


def _cargar_plugin_calendario(hermes_home: str):
    ruta = Path(hermes_home) / "plugins" / "hermeshq_ms365_calendar" / "__init__.py"
    if not ruta.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermeshq_ms365_calendar_directo", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


_HECHO_CALENDARIO = {
    "ms365_calendar_create_event": ("✅ Reunión creada en tu calendario", "crear la reunión"),
    "ms365_calendar_update_event": ("✅ Reunión actualizada en tu calendario", "modificar la reunión"),
    "ms365_calendar_delete_event": ("✅ Reunión cancelada y eliminada de tu calendario", "cancelar la reunión"),
}


def _texto_calendario(ejecucion: dict) -> str:
    resultado = ejecucion.get("resultado") or {}
    hecho, accion = _HECHO_CALENDARIO.get(ejecucion.get("herramienta"), ("✅ Listo", "hacer el cambio"))
    if not resultado.get("success"):
        return f"❌ No se pudo {accion}: {resultado.get('error') or 'Microsoft 365 no confirmó la operación'}. No se hizo ningún cambio."
    preview = ejecucion.get("preview") or {}
    evento = resultado.get("event") if isinstance(resultado.get("event"), dict) else {}
    actual = preview.get("evento_actual") if isinstance(preview.get("evento_actual"), dict) else {}
    asunto = evento.get("subject") or preview.get("asunto") or actual.get("asunto") or ""
    if ejecucion.get("herramienta") == "ms365_calendar_delete_event":
        inicio = (
            _hora_chile({"dateTime": actual["inicio"], "timeZone": actual.get("zona_horaria") or "UTC"})
            if actual.get("inicio")
            else ""
        )
    else:
        inicio = _hora_chile(evento.get("start")) or _hora_chile(preview.get("inicio") or preview.get("nuevo_inicio"))
    lineas = [f"{hecho} (confirmado por Microsoft 365)."]
    if asunto:
        lineas.append(f"**Reunión:** {asunto}")
    if inicio:
        lineas.append(f"**Inicio:** {inicio} (hora de Chile)")
    if evento.get("webLink"):
        lineas.append(f"**Enlace:** {evento['webLink']}")
    return "\n".join(lineas)


_HERRAMIENTAS_CALENDARIO_CON_VISTA = frozenset(
    {
        "ms365_calendar_create_event",
        "ms365_calendar_update_event",
        "ms365_calendar_delete_event",
        "ms365_calendar_respond_event",
    }
)
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_IMPORTANCIA = {"high": "alta", "normal": "normal", "low": "baja"}


def _hora_chile(valor) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if isinstance(valor, dict):
        texto, zona = str(valor.get("dateTime") or ""), str(valor.get("timeZone") or "UTC")
    else:
        texto, zona = str(valor or ""), "UTC"
    if not texto:
        return ""
    try:
        momento = datetime.fromisoformat(texto[:19])
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=ZoneInfo("UTC" if zona.upper() == "UTC" else zona))
        local = momento.astimezone(ZoneInfo("America/Santiago"))
    except (ValueError, KeyError):
        return texto[:16].replace("T", " ")
    return f"{_DIAS[local.weekday()]} {local:%d-%m-%Y %H:%M}"


def _lista(valores) -> str:
    return ", ".join(str(v) for v in valores if v) or "ninguno"


_SIN_RESERVA_DE_SALA = "(texto del lugar: no reserva la sala)"
_RESERVA_DE_SALA = "(se reserva como recurso; si está ocupada, la sala rechaza la invitación)"


def texto_vista_calendario(herramienta: str, vista: dict, aviso: str = "") -> str:
    lineas: list[str] = []
    if herramienta == "ms365_calendar_create_event":
        titulo, pregunta = "📅 **Vista previa de la reunión** (esto es exactamente lo que se creará):", "¿La creo?"
        lineas += [f"- **Título:** {vista.get('asunto') or '(sin título)'}"]
        lineas += [f"- **Inicio:** {_hora_chile(vista.get('inicio'))} (hora de Chile)"]
        lineas += [f"- **Fin:** {_hora_chile(vista.get('fin'))}"]
        opcionales = [str(a) for a in vista.get("opcionales") or []]
        obligatorios = [a for a in vista.get("asistentes") or [] if str(a) not in opcionales]
        lineas += [f"- **Invitados:** {_lista(obligatorios)}"]
        if opcionales:
            lineas += [f"- **Opcionales:** {_lista(opcionales)}"]
        if vista.get("reunion_teams") is not None:
            teams = "sí (el enlace se crea junto con la reunión)" if vista["reunion_teams"] else "no"
            lineas += [f"- **Reunión de Teams:** {teams}"]
    elif herramienta == "ms365_calendar_update_event":
        titulo, pregunta = "📅 **Vista previa del cambio** (esto es exactamente lo que se cambiará):", "¿Lo aplico?"
        actual = vista.get("evento_actual") if isinstance(vista.get("evento_actual"), dict) else {}
        if actual.get("asunto"):
            lineas.append(f"- **Reunión:** {actual['asunto']}")
        if actual.get("inicio"):
            zona = actual.get("zona_horaria") or "UTC"
            lineas.append(
                f"- **Horario actual:** {_hora_chile({'dateTime': actual['inicio'], 'timeZone': zona})} (hora de Chile)"
            )
        if vista.get("nuevo_asunto"):
            lineas.append(f"- **Nuevo título:** {vista['nuevo_asunto']}")
        if vista.get("nueva_sala"):
            lineas.append(f"- **Sala:** {vista['nueva_sala']} {_RESERVA_DE_SALA}")
        if vista.get("nueva_ubicacion"):
            lineas.append(f"- **Nuevo lugar:** {vista['nueva_ubicacion']} {_SIN_RESERVA_DE_SALA}")
        if vista.get("nuevo_inicio"):
            lineas.append(f"- **Nuevo inicio:** {_hora_chile(vista['nuevo_inicio'])} (hora de Chile)")
        if vista.get("nuevo_fin"):
            lineas.append(f"- **Nuevo fin:** {_hora_chile(vista['nuevo_fin'])}")
        if vista.get("asistentes_agregados"):
            lineas.append(f"- **Se agregan:** {_lista(vista['asistentes_agregados'])}")
        if vista.get("asistentes_QUITADOS_reciben_cancelacion"):
            quitados = _lista(vista["asistentes_QUITADOS_reciben_cancelacion"])
            lineas.append(f"- **Se quitan (reciben cancelación):** {quitados}")
    elif herramienta == "ms365_calendar_delete_event":
        titulo, pregunta = "🗑️ **Vista previa de la cancelación:**", "¿La cancelo?"
        lineas += [f"- **Reunión:** {vista.get('asunto') or '(sin título)'}"]
        if vista.get("inicio"):
            lineas.append(f"- **Inicio:** {_hora_chile(vista.get('inicio'))} (hora de Chile)")
        lineas.append(f"- **Reciben la cancelación:** {_lista(vista.get('asistentes_que_reciben_cancelacion') or [])}")
    else:
        titulo, pregunta = "📅 **Vista previa de tu respuesta a la invitación:**", "¿La envío?"
        lineas += [f"- **Respuesta:** {vista.get('accion') or ''}", f"- **Reunión:** {vista.get('asunto') or ''}"]
        if vista.get("inicio"):
            lineas.append(f"- **Inicio:** {_hora_chile(vista.get('inicio'))} (hora de Chile)")
        lineas.append(f"- **Avisa al organizador:** {'sí' if vista.get('avisa_al_organizador') else 'no'}")
        if vista.get("comentario"):
            lineas.append(f"- **Comentario:** {vista['comentario']}")
    if vista.get("sala"):
        lineas.append(f"- **Sala:** {vista['sala']} {_RESERVA_DE_SALA}")
    if vista.get("ubicacion"):
        lineas.append(f"- **Lugar:** {vista['ubicacion']} {_SIN_RESERVA_DE_SALA}")
    importancia = vista.get("importancia") or vista.get("nueva_importancia")
    if importancia:
        lineas.append(f"- **Importancia:** {_IMPORTANCIA.get(str(importancia).lower(), importancia)}")
    recordatorio = vista.get("recordatorio_minutos", vista.get("nuevo_recordatorio_minutos"))
    if recordatorio is not None:
        lineas.append(f"- **Recordatorio:** {recordatorio} minutos antes")
    if vista.get("descripcion"):
        lineas.append(f"- **Descripción:** {str(vista['descripcion'])[:300]}")
    verificacion = vista.get("verificacion_directorio")
    if isinstance(verificacion, dict):
        dudosos = [
            f"{correo}: {etiqueta}" for correo, etiqueta in verificacion.items() if str(etiqueta).startswith("⚠")
        ]
        if dudosos:
            lineas.append("\n⚠️ Revisa estos invitados: " + "; ".join(dudosos)[:400])
    return "\n".join(
        [titulo, *lineas, *(["", aviso] if aviso else []), "", f"{pregunta} Responde «sí» para confirmar."]
    )


def _marcar_vista_mostrada(payload: dict, texto: str) -> None:
    for archivo in (".calendario_pendiente.json", ".teams_pendiente.json"):
        ruta = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / archivo
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            if not isinstance(datos, dict) or str(datos.get("task") or "") != str(payload.get("task_id") or ""):
                continue
            datos["mostrada"] = texto
            tmp = ruta.with_suffix(".tmp")
            tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, ruta)
        except (OSError, ValueError):
            continue


_HERRAMIENTAS_TEAMS_CON_VISTA = frozenset({"teams_send_to_person", "teams_send_chat_message"})


def texto_vista_teams(herramienta: str, vista: dict) -> str:
    lineas: list[str] = []
    if herramienta == "teams_send_to_person":
        para = vista.get("para") if isinstance(vista.get("para"), dict) else {}
        nombre, correo = str(para.get("nombre") or ""), str(para.get("correo") or "")
        destino = f"{nombre} — {correo}" if nombre and correo else (nombre or correo or "(sin destinatario)")
        lineas.append(f"- **Para:** {destino}")
    else:
        lineas.append(f"- **Chat:** {vista.get('chat') or '(sin nombre)'}")
    if vista.get("mensaje"):
        lineas.append(f"- **Mensaje:** {vista['mensaje']}")
    if vista.get("archivo"):
        lineas.append(f"- **Archivo:** {Path(str(vista['archivo'])).name}")
    return "\n".join(
        [
            "💬 **Vista previa del mensaje de Teams** (esto es exactamente lo que se enviará):",
            *lineas,
            "",
            "¿Lo envío? Responde «sí» para confirmar.",
        ]
    )


def _ultima_vista_teams(mensajes: list, inicio: int):
    nombres = {}
    vista = None
    for m in mensajes[inicio + 1 :]:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                if isinstance(llamada, dict):
                    nombres[llamada.get("id")] = (llamada.get("function") or {}).get("name") or ""
        elif m.get("role") == "tool" and nombres.get(m.get("tool_call_id")) in _HERRAMIENTAS_TEAMS_CON_VISTA:
            try:
                datos = json.loads(m.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(datos, dict)
                and datos.get("requiere_aprobacion")
                and isinstance(datos.get("vista_previa"), dict)
            ):
                vista = (nombres[m.get("tool_call_id")], datos["vista_previa"])
            elif isinstance(datos, dict) and datos.get("success"):
                vista = None
    return vista


def vista_teams_del_servidor(mensajes: list, inicio: int) -> str | None:
    vista = _ultima_vista_teams(mensajes, inicio)
    return texto_vista_teams(*vista) if vista else None


def _cargar_plugin_teams(hermes_home: str):
    ruta = Path(hermes_home) / "plugins" / "hermeshq_ms365_teams" / "__init__.py"
    if not ruta.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermeshq_ms365_teams_directo", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _texto_teams(ejecucion: dict) -> str:
    resultado = ejecucion.get("resultado") or {}
    if not resultado.get("success"):
        return (
            f"❌ No se pudo enviar el mensaje por Teams: {resultado.get('error') or 'Microsoft 365 no confirmó el envío'}. "
            "No se envió nada."
        )
    preview = ejecucion.get("preview") or {}
    para = preview.get("para") if isinstance(preview.get("para"), dict) else {}
    destino = str(para.get("nombre") or preview.get("chat") or "")
    lineas = ["✅ Mensaje enviado por Teams (confirmado por Microsoft 365)."]
    if destino:
        lineas.append(f"**Para:** {destino}")
    if preview.get("mensaje"):
        lineas.append(f"**Mensaje:** {preview['mensaje']}")
    return "\n".join(lineas)


def _teams_aprobado_directo(payload: dict) -> dict | None:
    if not _chat_con_usuario(payload):
        return None
    try:
        plugin = _cargar_plugin_teams(payload["hermes_home"])
        ejecucion = plugin.ejecutar_aprobacion_pendiente() if plugin else None
    except Exception as exc:  # noqa: BLE001
        _diag(f"aprobación directa de Teams no disponible: {exc}")
        return None
    if not ejecucion or (ejecucion.get("resultado") or {}).get("requiere_aprobacion"):
        return None
    llamada_id = f"teams-directo-{payload.get('task_id')}"
    texto = _texto_teams(ejecucion)
    mensajes = [
        {"role": "user", "content": payload.get("prompt") or ""},
        *_mensajes_herramienta(ejecucion["herramienta"], ejecucion["args"], ejecucion["resultado"], llamada_id),
        {"role": "assistant", "content": texto},
    ]
    return build_result_payload({"api_calls": 0}, texto, mensajes, _extract_tool_calls(mensajes))


_SALA_PEDIDA = re.compile(
    r"\bsala\s+(?:de\s+reuniones\s+)?([A-Za-zÁÉÍÓÚÑáéíóúñ0-9][\wÁÉÍÓÚÑáéíóúñ]*(?:\s+(?!con\b|para\b|a\b|el\b|la\b|de\b|y\b|"
    r"en\b|por\b|que\b)[\wÁÉÍÓÚÑáéíóúñ]+){0,3})",
    re.IGNORECASE,
)


def _compacto(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", sin_acentos.lower())


def sala_pedida_sin_incluir(mensaje: str, vista: dict) -> str | None:
    coincidencia = _SALA_PEDIDA.search(_texto_propio_del_usuario(mensaje) or "")
    if not coincidencia or not isinstance(vista, dict):
        return None
    sala = coincidencia.group(1).strip()
    clave = _compacto(sala)
    if len(clave) < 3:
        return None
    incluidos = [
        *(vista.get("asistentes") or []),
        *(vista.get("asistentes_agregados") or []),
        *(vista.get("asistentes_finales") or []),
        vista.get("ubicacion") or "",
        vista.get("nueva_ubicacion") or "",
        vista.get("sala") or "",
        vista.get("nueva_sala") or "",
    ]
    actual = vista.get("evento_actual") if isinstance(vista.get("evento_actual"), dict) else {}
    incluidos.append(actual.get("ubicacion") or "")
    return None if any(clave in _compacto(x) for x in incluidos) else sala


_QUITAR_PERSONA = re.compile(
    r"\b(?:elimin\w*|quit\w*|saca\w*|remuev\w*|borra\w*)\s+(?:a\s+)?"
    r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+)?)"
)


def persona_a_quitar_sin_incluir(mensaje: str, herramienta: str, vista: dict) -> str | None:
    if herramienta != "ms365_calendar_update_event" or not isinstance(vista, dict):
        return None
    coincidencia = _QUITAR_PERSONA.search(_texto_propio_del_usuario(mensaje) or "")
    if not coincidencia:
        return None
    persona = coincidencia.group(1).strip()
    quitados = [str(x) for x in (vista.get("asistentes_QUITADOS_reciben_cancelacion") or [])]
    palabras = [_compacto(p) for p in persona.split() if len(p) >= 3]
    if not palabras or any(any(p in _compacto(q) for p in palabras) for q in quitados):
        return None
    return persona


def _ultima_vista_calendario(mensajes: list, inicio: int):
    nombres = {}
    vista = None
    for m in mensajes[inicio + 1 :]:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                if isinstance(llamada, dict):
                    nombres[llamada.get("id")] = (llamada.get("function") or {}).get("name") or ""
        elif m.get("role") == "tool" and nombres.get(m.get("tool_call_id")) in _HERRAMIENTAS_CALENDARIO_CON_VISTA:
            try:
                datos = json.loads(m.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(datos, dict)
                and datos.get("requiere_aprobacion")
                and isinstance(datos.get("vista_previa"), dict)
            ):
                vista = (nombres[m.get("tool_call_id")], datos["vista_previa"])
            elif isinstance(datos, dict) and datos.get("success"):
                vista = None
    return vista


def vista_calendario_del_servidor(mensajes: list, inicio: int, mensaje: str = "") -> str | None:
    vista = _ultima_vista_calendario(mensajes, inicio)
    if not vista:
        return None
    sala = sala_pedida_sin_incluir(mensaje, vista[1]) if vista[0] != "ms365_calendar_delete_event" else None
    aviso = (
        f"⚠️ Pediste la sala «{sala}» y esta vista previa no la incluye: no quedaría reservada. Si la quieres, dime "
        "«agrega la sala» y la sumo como recurso con su correo antes de confirmar."
        if sala
        else ""
    )
    persona = persona_a_quitar_sin_incluir(mensaje, vista[0], vista[1])
    if persona:
        aviso = (aviso + "\n" if aviso else "") + (
            f"⚠️ Pediste quitar a «{persona}» y esta vista previa no lo incluye: seguiría en la reunión. Dime "
            "«quita a esa persona» y lo sumo al cambio antes de confirmar."
        )
    return texto_vista_calendario(*vista, aviso=aviso)


def _calendario_aprobado_directo(payload: dict) -> dict | None:
    if not _chat_con_usuario(payload):
        return None
    try:
        plugin = _cargar_plugin_calendario(payload["hermes_home"])
        ejecucion = plugin.ejecutar_aprobacion_pendiente() if plugin else None
    except Exception as exc:  # noqa: BLE001
        _diag(f"aprobación directa de calendario no disponible: {exc}")
        return None
    if not ejecucion or (ejecucion.get("resultado") or {}).get("requiere_aprobacion"):
        return None
    llamada_id = f"calendario-directo-{payload.get('task_id')}"
    texto = _texto_calendario(ejecucion)
    mensajes = [
        {"role": "user", "content": payload.get("prompt") or ""},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": llamada_id,
                    "type": "function",
                    "function": {
                        "name": ejecucion["herramienta"],
                        "arguments": json.dumps(ejecucion["args"], ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": ejecucion["herramienta"],
            "tool_call_id": llamada_id,
            "content": json.dumps(ejecucion["resultado"], ensure_ascii=False),
        },
        {"role": "assistant", "content": texto},
    ]
    return build_result_payload({"api_calls": 0}, texto, mensajes, _extract_tool_calls(mensajes))


_ARCHIVOS = (
    r"presentaci[óo]n(es)?|pptx?|diapositivas?|l[áa]minas?|slides?|deck|documentos?|word|docx|excel|xlsx|"
    r"planillas?|hoja de c[áa]lculo|pdf|archivos?|dashboard|tablero|skills?|habilidad(es)?"
)
_VERBOS_AR = r"(a|ar|alo|ala|alos|alas|ale|ame|ales|es|en|arlo|arla|arle|arles)"
_PEDIDOS_ACCION = (
    (
        "correo",
        re.compile(
            r"\b(env[íi](a|ar|ale|ales|alo|ala|es|en)|mand(a|ar|ale|ales|alo|es)|m[áa]ndale|respond(e|er|ele|as)|"
            r"resp[óo]ndele|contest(a|ar|ale|es)|cont[ée]stale|reenv[íi](a|ar|ale|alo|es))\b|"
            r"\b(d[ée]ja(me|le)?|dejar|crea|crear|cr[ée]ame|guarda|guardar|prepara|preparar|prep[áa]rame)\s+"
            r"(\w+\s+){0,2}borrador",
            re.I,
        ),
        re.compile(r"\b(correos?|mails?|e-?mails?)\b", re.I),
    ),
    (
        "programacion",
        re.compile(
            r"(?<!el )(?<!un )(?<!del )\b(program(a|ar|ame|es|alo)|progr[áa]mame|agend(a|ar|ame|es|alo)|ag[ée]ndame|"
            r"recu[ée]rd(ame|ale|ales)|av[íi]same|invit(a|ar|es)\b.{0,40}reuni[óo]n|"
            r"(crea|crear|cr[ée]ame|configura|configurar|deja|dejar|pon|ponme|agrega)\b.{0,30}"
            r"(recordatorio|cron|tarea programada|alerta|reuni[óo]n|cita|evento))\b|"
            r"\b(paus(a|ar|ala|alo)|reanud(a|ar|ala|alo)|reactiv(a|ar|ala|alo)|det[ée]n(la|lo)?|detener|"
            r"elimin(a|ar|ala|alo)|borr(a|ar|ala|alo)|cancel(a|ar|ala|alo)|desactiv(a|ar|ala|alo))\b.{0,40}"
            r"(tarea programada|tarea recurrente|programaci[óo]n|\bcron\b|recordatorio)|"
            r"\b(elimin(a|ar|ala|alo)|borr(a|ar|ala|alo)|paus(a|ar|ala|alo)|reanud(a|ar|ala|alo)|cancel(a|ar|ala|alo)|"
            r"desactiv(a|ar|ala|alo))\s+(la|el)\s+de\s+las?\s+\d{1,2}[:h.]\d{2}",
            re.I,
        ),
        None,
    ),
    (
        "subida",
        re.compile(
            r"\b(sub(e|ir|elo|ela|as)|s[úu]belo|s[úu]bela|compart(e|ir|elo|ela|as)|comp[áa]rtelo|comp[áa]rtela|comp[áa]rteme|"
            r"public(a|ar|alo)|guard(a|ar|alo|ala))\b",
            re.I,
        ),
        re.compile(r"\b(sharepoint|onedrive|drive|carpeta|biblioteca|sitio|canal|enlace|link|v[íi]nculo)\b", re.I),
    ),
    (
        "edicion",
        re.compile(
            r"\b(edit|modific|cambi|reemplaz|actualiz|agreg|quit|elimin|borr|renombr|ajust|insert|incorpor|redise[ñn])"
            + _VERBOS_AR
            + r"\b|\bmejor(a|ar|alo|ala|ame|arlo|arla)\b|\bmej[óo]ra(lo|la|me)\b|\b(añad|anad)(e|ir|elo|ela|ele|as|irle)\b|\bcorrig(e|elo|ela|ele|as)\b|\bcorregir\b|"
            r"\bsustitu(ye|ir|yas)\b|\b(modifiqu|actualic|agregu|reemplac|quit|borr)(es|en)\b|"
            r"\b(c[áa]mbi|agr[ée]g|ed[íi]t|añ[áa]d|qu[íi]t|elim[íi]n|corr[íi]g|actual[íi]z|modif[íi]c|reempl[áa]z|"
            r"b[óo]rr|aj[úu]st|ren[óo]mbr)(alo|ala|alos|alas|ale|ales|elo|ela|ele|ame|eme)\b",
            re.I,
        ),
        re.compile(r"\b(" + _ARCHIVOS + r")\b", re.I),
    ),
    (
        "creacion",
        re.compile(
            r"\b(cre(a|ar|ame|alo|es)|cr[ée]ame|cr[ée]alo|gener(a|ar|ame|alo|es)|gen[ée]rame|gen[ée]ralo|"
            r"arm(a|ar|ame|es)|[áa]rmame|prepar(a|ar|ame|es)|prep[áa]rame|elabor(a|ar|es)|constru(ye|ir|yas)|"
            r"haz|hazme|hacer|hagas|export(a|ar|alo)|exp[óo]rtalo|conviert(e|elo|ela)|convertir|p[áa]salo a|pasa(lo)? a)\b",
            re.I,
        ),
        re.compile(r"\b(" + _ARCHIVOS + r")\b", re.I),
    ),
)
_AFIRMA_HECHO = re.compile(
    r"\b(editad|modificad|cambiad|actualizad|agregad|añadid|reemplazad|sustituid|corregid|eliminad|quitad|borrad|"
    r"renombrad|insertad|incorporad|cread|generad|guardad|preparad|armad|elaborad|exportad|convertid|enviad|"
    r"respondid|contestad|reenviad|subid|compartid|publicad|programad|agendad|invitad|aplicad|movid|reprogramad|"
    r"cancelad|ejecutad|redise[ñn]ad|mejorad|pausad|reanudad|reactivad|desactivad|detenid)(o|a|os|as)\b|"
    r"\b(edit[ée]|modifiqu[ée]|cambi[ée]|actualic[ée]|agregu[ée]|añad[íi]|reemplac[ée]|correg[íi]|elimin[ée]|"
    r"quit[ée]|borr[ée]|renombr[ée]|cre[ée]|gener[ée]|guard[ée]|prepar[ée]|arm[ée]|export[ée]|convert[íi]|"
    r"envi[ée]|respond[íi]|contest[ée]|reenvi[ée]|sub[íi]|compart[íi]|publiqu[ée]|program[ée]|agend[ée]|invit[ée]|"
    r"apliqu[ée]|mov[íi]|reprogram[ée]|cancel[ée]|ejecut[ée]|redise[ñn][ée]|mejor[ée]|paus[ée]|reanud[ée]|"
    r"reactiv[ée]|desactiv[ée]|deten[íi])\b|"
    r"\bse (edit|modific|cambi|actualiz|agreg|añadi|reemplaz|corrigi|elimin|quit|borr|renombr|cre|gener|guard|"
    r"prepar|export|convirti|envi|respondi|contest|reenvi|subi|comparti|public|program|agend|invit)[óo]\b|"
    r"\bhe (editado|modificado|cambiado|actualizado|agregado|añadido|creado|generado|guardado|preparado|enviado|"
    r"respondido|reenviado|subido|compartido|programado|agendado)\b|"
    r"\bte (avisaré|recordaré|escribiré) (cada|todos|el|ma[ñn]ana|a las|en)\b|"
    r"^\s*MEDIA:",
    re.I | re.M,
)
_MARCA_FUERTE = re.compile(
    r"\blisto\b|✅|\bhecho\b|\bya (est[áa]|qued[óo]|lo|la|los|las|tienes)\b|nueva versi[óo]n|^\s*MEDIA:", re.I | re.M
)
_NEGACION = re.compile(r"\b(no|sin|nunca|ni|tampoco|a[úu]n no|todav[íi]a no)\b", re.I)
_NO_PUDO = re.compile(
    r"\bno (pude|logr[ée]|fue posible|se pudo|he podido|puedo|tengo acceso|tengo permisos?|tengo (la |una )?herramienta|"
    r"est[áa] disponible|me es posible|encontr[ée]|encuentro)\b",
    re.I,
)
_PROPUESTA = re.compile(
    r"te propongo|propuesta|si (me )?confirmas|antes de (hacer|editar|enviar|crear|programar|subir|cambiar)|"
    r"¿\s*(lo|la|los|las|te|le)\s+\w+|¿\s*quieres que|¿\s*procedo|¿\s*confirmas|¿\s*te parece|¿\s*prefieres",
    re.I,
)
_PREGUNTA_DE_CIERRE = re.compile(r"algo m[áa]s|otra cosa|otro cambio|alg[úu]n (otro )?ajuste|en qu[ée] m[áa]s", re.I)
_ORACIONES = re.compile(r"(?<=[.!?…])\s+|\n+")
_HECHO_PEDIDO = {
    "correo": ("enviar o responder el correo", "el envío del correo"),
    "programacion": ("programar la tarea o la reunión", "la programación"),
    "subida": ("subir o compartir el archivo", "la subida del archivo"),
    "edicion": ("editar el archivo", "el cambio"),
    "creacion": ("crear el archivo", "el archivo"),
    "vista_calendario": ("preparar la reunión", "la reunión"),
    "busqueda": ("buscarlo (dijiste que no lo encontraste)", "la búsqueda"),
    "anuncio": ("lo que el usuario acaba de confirmar", "lo que confirmaste"),
}

_PEDIDO_CALENDARIO = re.compile(
    r"reuni[oó]n|\bcita\b|\bevento\b|calendario|invitaci[oó]n|\bmeeting\b|\bagend|\bag[ée]nd|reagend|reprogram",
    re.IGNORECASE,
)
_VISTA_PREVIA_DE_CALENDARIO = re.compile(
    r"vista previa[^\n]{0,40}\b(reuni[oó]n|cita|evento|calendario|invitaci[oó]n|cancelaci[oó]n)\b", re.IGNORECASE
)
_CAMPOS_VISTA_CALENDARIO = re.compile(
    r"(?:^|\n)\W*(?:nuev[oa]\s+)?(inicio|fin|invitad\w+|asistentes|lugar|fecha(?:\s*(?:/|y)\s*hora)?|horario|hora|cu[aá]ndo|antes|despu[eé]s|cita|"
    r"reuni[oó]n|evento)"
    r"\s*\**\s*:",
    re.IGNORECASE,
)
_VISTA_ESCRITA = re.compile(r"vista previa|\bt[ií]tulo\s*\**\s*:|\basunto\s*\**\s*:", re.IGNORECASE)
_PREGUNTA_CONFIRMACION = re.compile(r"¿[^?¿]{2,120}\?|\?\s*$", re.MULTILINE)
_NOTA_VISTA_CALENDARIO = (
    "[Nota del sistema] Escribiste la vista previa de la reunión a mano, sin llamar la herramienta de calendario, "
    "así que el servidor no la registró y la confirmación del usuario no podría ejecutarla. Llama ahora "
    "ms365_calendar_create_event (o la de modificar o cancelar, según el pedido) SIN aprobado_por_el_usuario "
    "para que el servidor genere la vista previa, y muestra al usuario esa vista previa tal cual."
    + _SIN_MENCIONAR_REVISION
)


_AVISO_VISTA_SIN_REGISTRAR = (
    "⚠️ No logré preparar el cambio en tu calendario: la vista previa no quedó registrada en Microsoft 365, así que "
    "un «sí» no haría nada. Pídemelo de nuevo nombrando la reunión (por ejemplo: «mueve la reunión X a las 16:00»)."
)


_CALENDARIO_EN_RESPUESTA = re.compile(
    r"reuni[oó]n|\bcita\b|\bevento\b|calendario|\basistentes\b|\binvitad[oa]s\b", re.IGNORECASE
)


_LECTURAS_CALENDARIO = frozenset(
    {"ms365_calendar_list_events", "ms365_calendar_get_event", "ms365_calendar_find_free_time"}
)


_CONFIRMA_CAMBIO_CALENDARIO = re.compile(
    r"¿\s*(?:(?:lo|la|los|las)\s+)?(?:cancelo|elimino|borro|muevo|creo|agendo|cambio|reprogramo|aplico|actualizo)\b"
    r"[^?¿]{0,160}\?|"
    r"¿\s*(?:eliminar|cancelar|borrar|mover|crear|agendar|reprogramar)\s+(?:la|el|esta|este|esa|ese)\b[^?¿]{0,160}\?|"
    r"¿\s*(?:confirmas|procedo)\b[^?¿]{0,160}\?|"
    r"¿\s*(?:quieres|deseas) que (?:la|lo) (?:cancele|elimine|borre|mueva|cree|agende|cambie|reprograme)\b[^?¿]{0,160}\?",
    re.IGNORECASE,
)


_PEDIDO_CRON = re.compile(
    r"recordatorio|\bcron\b|tarea programada|\bcada (d[ií]a|semana|hora|mes|lunes|martes|mi[ée]rcoles|jueves|"
    r"viernes)|\bcada \d+ (minutos|horas|d[ií]as)|todos los (d[ií]as|lunes|martes|mi[ée]rcoles|jueves|viernes)",
    re.IGNORECASE,
)
_REUNION_EN_PEDIDO = re.compile(r"reuni[oó]n|\bcita\b|\bevento\b|invitaci[oó]n|\bmeeting\b", re.IGNORECASE)
_REUNION_EN_PREGUNTA = re.compile(r"reuni[oó]n|\bcita\b|\bevento\b|invitaci[oó]n|\bE\d{1,2}\b", re.IGNORECASE)


def _confirmacion_de_calendario_a_mano(mensaje: str, respuesta: str, mensajes: list, inicio: int) -> bool:
    propio = _texto_propio_del_usuario(mensaje)
    if not _REUNION_EN_PEDIDO.search(propio) and not _PEDIDO_CALENDARIO.search(propio):
        return False
    if _HABLA_DE_SKILL.search(propio) or (_PEDIDO_CRON.search(propio) and not _REUNION_EN_PEDIDO.search(propio)):
        return False
    preguntas = [c.group(0) for c in _CONFIRMA_CAMBIO_CALENDARIO.finditer(respuesta or "")]
    if not preguntas:
        return False
    consulto = any(nombre in _LECTURAS_CALENDARIO for nombre, _, _ in resultados_de_herramientas(mensajes, inicio))
    return consulto or any(_REUNION_EN_PREGUNTA.search(p) for p in preguntas)


def _plano_para_comparar(texto: str) -> str:
    return (
        re.sub(r"\s+", " ", unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode())
        .lower()
        .strip()
    )


_NOTA_REPITE = (
    "[Nota del sistema] Tu respuesta repite casi palabra por palabra una que ya diste antes en esta conversación y no "
    "atiende el último mensaje del usuario, que es: «{mensaje}». Respóndelo ahora, con lo que ese mensaje dice o "
    "pide; si necesitas algo que no tienes, pídelo. No repitas la respuesta anterior."
) + _SIN_MENCIONAR_REVISION
_AVISO_REPITE = (
    "⚠️ No logré responder a tu último mensaje: mi respuesta volvía a repetir una anterior. ¿Me lo puedes escribir de "
    "otra forma o decirme qué necesitas que haga con eso?"
)


def respuesta_repetida_de_antes(mensaje: str, respuesta: str, mensajes: list, inicio: int) -> bool:
    actual = _plano_para_comparar(respuesta)
    if len(actual) < 120 or re.search(r"\b(repite|repetir|otra vez|de nuevo|lo mismo)\b", mensaje or "", re.I):
        return False
    asistente = [m for m in mensajes[:inicio] if isinstance(m, dict) and m.get("role") == "assistant"]
    for previo in asistente[:-1]:
        contenido = previo.get("content")
        if not isinstance(contenido, str):
            continue
        anterior = _plano_para_comparar(contenido)
        if len(anterior) < 120:
            continue
        if actual == anterior or actual in anterior or anterior in actual:
            return True
        if difflib.SequenceMatcher(None, actual[:2500], anterior[:2500], autojunk=False).ratio() >= 0.93:
            return True
    return False


_ASUNTO_DE_CORREO = re.compile(r"(?:^|\n)\W*asunto\s*\**\s*:", re.IGNORECASE)
_MARCA_DE_CORREO = re.compile(
    r"(?:^|\n)\W*(?:para|cc|destinatari\w+)\s*\**\s*:|\b(?:estimad[oa]s?|saludos|atentamente|cordialmente)\b|(?:^|\n)\s*hola\b",
    re.IGNORECASE,
)
_CAMPO_PROPIO_DE_CALENDARIO = re.compile(
    r"(?:^|\n)\W*(?:nuev[oa]\s+)?(?:inicio|fin|invitad\w+|asistentes|lugar|cu[aá]ndo|fecha|horario|hora)\s*\**\s*:",
    re.IGNORECASE,
)


def _es_borrador_de_correo(respuesta: str) -> bool:
    if _VISTA_PREVIA_DE_CALENDARIO.search(respuesta) or _CAMPO_PROPIO_DE_CALENDARIO.search(respuesta):
        return False
    return bool(_ASUNTO_DE_CORREO.search(respuesta) and _MARCA_DE_CORREO.search(respuesta))


def vista_calendario_a_mano(mensaje: str, respuesta: str, mensajes: list, inicio: int) -> bool:
    if not respuesta:
        return False
    if not (
        _PEDIDO_CALENDARIO.search(mensaje or "")
        or (_CALENDARIO_EN_RESPUESTA.search(respuesta) and not _PEDIDO_ENVIO.search(mensaje or ""))
    ):
        return False
    vista_escrita = _VISTA_ESCRITA.search(respuesta) and _PREGUNTA_CONFIRMACION.search(respuesta)
    if vista_escrita and _es_borrador_de_correo(respuesta):
        vista_escrita = False
    if vista_escrita and not _PEDIDO_CALENDARIO.search(_texto_propio_del_usuario(mensaje)):
        campos = {m.group(1).lower() for m in _CAMPOS_VISTA_CALENDARIO.finditer(respuesta)}
        if len(campos) < 2 and not _VISTA_PREVIA_DE_CALENDARIO.search(respuesta):
            vista_escrita = False
    if not vista_escrita and not _confirmacion_de_calendario_a_mano(mensaje, respuesta, mensajes, inicio):
        return False
    for m in mensajes[inicio + 1 :]:
        for llamada in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            nombre = (llamada.get("function") or {}).get("name") or ""
            if nombre.startswith("ms365_calendar_") and nombre not in _LECTURAS_CALENDARIO:
                return False
    return True


_CAMPO_PARA = re.compile(r"(?:^|\n)[\s>*_\-]*para\s*\**\s*:\s*\**\s*(.+)", re.I)
_CAMPO_CC = re.compile(r"(?:^|\n)[\s>*_\-]*cc\s*\**\s*:\s*\**\s*(.+)", re.I)
_LINEA_SALUDO = re.compile(
    r"^[\s>*_\-]*(?:muy\s+)?(?:buen(?:os|as)?\s+(?:d[ií]as?|tardes?|noches?)|buenas|hola|estimad[oa]s?|querid[oa]s?)"
    r"[\s,:]+(?P<nombres>[^\n]{1,80}?)\s*[,.:;!]*\s*$",
    re.I | re.M,
)
_PALABRAS_SIN_NOMBRE = frozenset(
    [
        "a",
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "y",
        "e",
        "equipo",
        "todos",
        "todas",
        "estimados",
        "estimadas",
        "amigos",
        "team",
        "tod@s",
        "ustedes",
    ]
)
_PARA_YO = re.compile(r"\b(a m[ií] mism[oa]|m[ií] mism[oa]|yo|mi buz[oó]n|a m[ií])\b", re.I)
_ID_DE_CORREO = re.compile(r"(?<![\w&/])#(\d{2,6})\b")
_ASUNTO_CITADO = re.compile(r"[«\"“]([^»\"”\n]{6,200})[»\"”]")


def _sin_acentos(texto: str) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii").lower()


def _palabras_de_nombre(texto: str) -> list[str]:
    return [p for p in re.findall(r"[a-z]{2,}", _sin_acentos(texto)) if p not in _PALABRAS_SIN_NOMBRE]


def _hay_saludo(respuesta: str) -> bool:
    return bool(_LINEA_SALUDO.search(respuesta or ""))


def borrador_incoherente(respuesta: str) -> str | None:
    if not respuesta or not _es_borrador_de_correo(respuesta):
        return None
    para = _CAMPO_PARA.search(respuesta)
    if not para:
        return None
    linea_para = para.group(1).strip()
    if _PARA_YO.search(linea_para) or not re.search(
        r"[A-Za-zÁÉÍÓÚáéíóúñÑ]{2,}\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]{2,}", re.sub(r"\S+@\S+", "", linea_para)
    ):
        return None
    destino = set(_palabras_de_nombre(re.sub(r"\S+@\S+", " ", linea_para)))
    cc = _CAMPO_CC.search(respuesta)
    if cc:
        destino |= set(_palabras_de_nombre(re.sub(r"\S+@\S+", " ", cc.group(1))))
    for saludo in _LINEA_SALUDO.finditer(respuesta):
        nombres = _palabras_de_nombre(saludo.group("nombres"))
        if not nombres or len(nombres) > 6:
            continue
        if not any(
            n in destino or any(len(n) >= 4 and (n.startswith(d) or d.startswith(n)) for d in destino) for n in nombres
        ):
            return (
                f"El saludo nombra a «{saludo.group('nombres').strip()}» pero el borrador va Para: «{linea_para}»: "
                "el destinatario y el saludo no coinciden."
            )
        return None
    return None


def _normaliza_asunto(texto: str) -> str:
    base = _sin_acentos(texto)
    while True:
        sin = re.sub(r"^\s*(re|rv|fw|fwd)\s*:\s*", "", base)
        if sin == base:
            break
        base = sin
    return " ".join(re.findall(r"[a-z0-9]+", base))


def _asunto_coincide(citado: str, real: str) -> bool:
    a, b = _normaliza_asunto(citado), _normaliza_asunto(real)
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.6


def ids_de_correo_incoherentes(respuesta: str, mapa_ids: dict) -> list[str]:
    problemas: list[str] = []
    if not respuesta or not mapa_ids:
        return problemas
    for linea in respuesta.splitlines():
        ids = [f"#{n}" for n in _ID_DE_CORREO.findall(linea)]
        citados = [c.strip() for c in _ASUNTO_CITADO.findall(linea)]
        if len(ids) != 1 or not citados:
            continue
        real = mapa_ids.get(ids[0])
        if real is None:
            continue
        if not any(_asunto_coincide(c, real) for c in citados):
            problemas.append(f"{ids[0]} es «{real}», no «{citados[0]}»")
    return list(dict.fromkeys(problemas))


def incoherencia_de_correo(respuesta: str, mapa_ids: dict | None = None) -> str | None:
    partes = []
    borrador = borrador_incoherente(respuesta)
    if borrador:
        partes.append(borrador)
    ids = ids_de_correo_incoherentes(respuesta, mapa_ids or {})
    if ids:
        partes.append("IDs que no corresponden: " + "; ".join(ids) + ".")
    return " ".join(partes) or None


_PEDIDO_ENVIO = re.compile(r"\b(mensaje|correo|mail|email|teams|chat)\b", re.I)
_VERBO_ENVIO_PEDIDO = re.compile(r"\b(env[ií]\w*|mand\w*|respond\w*|contest\w*|reenv[ií]\w*)\b", re.I)
_PIDE_VISTA_PREVIA = re.compile(r"\bvista previa\b", re.I)
_SOLO_REDACCION = re.compile(r"\b(redactad[oa]|tipo correo|para copiar|redact[ae]\w*)\b", re.I)
_VISTA_ENVIO_ESCRITA = re.compile(
    r"vista previa|borrador|(^|\n)\W*(chat|para|asunto|destinatari[oa]s?|mensaje|cc)\s*\**\s*:", re.I
)
_PIDE_ENVIO_CONFIRMACION = re.compile(r"¿[^?¿]{0,80}\b(env[ií]\w*|mand\w*|confirm\w*)\b[^?¿]{0,80}\?", re.I)
_HERRAMIENTAS_ENVIO = frozenset(
    {"teams_send_to_person", "teams_send_chat_message", "ms365_mail_send", "ms365_mail_reply", "ms365_mail_forward"}
)
_ES_TEAMS = re.compile(r"\bteams\b|\bchat\b", re.I)


def vista_envio_a_mano(mensaje: str, respuesta: str, mensajes: list, inicio: int) -> str | None:
    propio = _texto_propio_del_usuario(mensaje).strip()
    if _HABLA_DE_SKILL.search(propio) or _es_pregunta(propio):
        return None
    borrador_escrito = bool(
        _es_borrador_de_correo(respuesta or "") and _CAMPO_PARA.search(respuesta or "") and _hay_saludo(respuesta)
    )
    if not _PEDIDO_ENVIO.search(propio) and not borrador_escrito:
        return None
    pide_envio = _VERBO_ENVIO_PEDIDO.search(propio) and not _SOLO_REDACCION.search(propio)
    if not (pide_envio or _PIDE_VISTA_PREVIA.search(propio) or borrador_escrito):
        return None
    if not (_VISTA_ENVIO_ESCRITA.search(respuesta or "") and _PIDE_ENVIO_CONFIRMACION.search(respuesta or "")):
        return None
    for m in mensajes[inicio + 1 :]:
        for llamada in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            if ((llamada.get("function") or {}).get("name") or "") in _HERRAMIENTAS_ENVIO:
                return None
    if borrador_escrito and not _PEDIDO_ENVIO.search(propio):
        return "correo"
    return "teams" if _ES_TEAMS.search(propio) else "correo"


_NOTA_VISTA_ENVIO = {
    "teams": (
        "teams_send_chat_message con chat_nombre = el nombre del chat tal como lo dijo el usuario (o "
        "teams_send_to_person si es una persona) y el texto exacto del mensaje, solo lo que debe decir, sin las "
        "instrucciones del usuario"
    ),
    "correo": (
        "ms365_mail_send (o ms365_mail_reply / ms365_mail_forward si responde o reenvía) con los destinatarios, el "
        "asunto y el cuerpo del pedido"
    ),
}
_AVISO_VISTA_ENVIO_SIN_REGISTRAR = (
    "⚠️ No logré preparar el envío: la vista previa no quedó registrada en Microsoft 365, así que un «sí» no enviaría "
    "nada. Pídemelo de nuevo nombrando el chat o la persona y el texto exacto."
)


def _mapa_ids_del_chat(payload: dict) -> dict:
    try:
        plugin = _cargar_plugin_correo(payload["hermes_home"])
        mapa = plugin.mapa_ids_correo() if plugin and hasattr(plugin, "mapa_ids_correo") else {}
    except Exception as exc:  # noqa: BLE001
        _diag(f"mapa de #ID no disponible: {exc}")
        return {}
    return mapa if isinstance(mapa, dict) else {}


_MAPA_IDS_CORREO: dict = {}

_AVISO_CORREO_INCOHERENTE = (
    "⚠️ No pude dejar coherente la respuesta sobre ese correo (el destinatario, el saludo o el #ID no coinciden con el "
    "correo real), así que no te muestro un borrador que podría ir a la persona equivocada. Dime el asunto o el "
    "remitente exacto del correo que quieres responder y lo preparo desde cero."
)


def _nota_correo_incoherente(respuesta: str) -> str:
    detalle = incoherencia_de_correo(respuesta, _MAPA_IDS_CORREO) or ""
    return (
        f"[Nota del sistema] Tu respuesta tiene una incoherencia verificable: {detalle} Corrígela AHORA: el destinatario de "
        "una respuesta es el remitente del correo respondido (llama ms365_mail_reply con el message_id exacto: la "
        "herramienta arma Para/CC del hilo y la muestra la plataforma; no escribas el borrador a mano); el saludo "
        "nombra a quien lo recibe; un #ID solo se afirma si una herramienta lo devolvió en este turno "
        "(ms365_correo_sync accion='referencia' o ms365_mail_get con el #ID) y se describe con el asunto que esa "
        "herramienta devolvió. Si el usuario puede referirse a otro correo, consúltalo con la herramienta o pregúntale; "
        "no adivines." + _SIN_MENCIONAR_REVISION
    )


def _nota_vista_envio(canal: str) -> str:
    return (
        "[Nota del sistema] Escribiste a mano la vista previa del envío, sin llamar la herramienta, así que el servidor "
        f"no la registró y el «sí» del usuario no podría enviarla. Llama AHORA {_NOTA_VISTA_ENVIO[canal]}, SIN "
        "aprobación: la herramienta devuelve la vista previa real y no envía nada hasta el «sí». No escribas la vista "
        "previa: la muestra la plataforma." + _SIN_MENCIONAR_REVISION
    )


_PREGUNTA_INFORMATIVA = re.compile(
    r"^\s*¿?\s*(qu[ée]|c[óo]mo|cu[áa]l(es)?|por qu[ée]|cu[áa]ndo|d[óo]nde|qui[ée]n(es)?|cu[áa]nt[oa]s?)\b", re.I
)


_NEGACION_DEL_VERBO = re.compile(r"\b(no|nunca|jam[aá]s|ni|sin)\s+((me|te|le|les|lo|la|los|las|se|nos)\s+){0,2}$", re.I)


_RESPONDE_EN_EL_CHAT = re.compile(
    r"\s+(indicando|diciendo|explicando|informando|que\b|en (este|el) chat|aqu[ií]\b|al usuario|brevemente)", re.I
)


def _verbo_no_negado(verbo: re.Pattern, texto: str) -> bool:
    for c in verbo.finditer(texto):
        if _NEGACION_DEL_VERBO.search(texto[max(0, c.start() - 40) : c.start()]):
            continue
        if re.match(r"(respond|contest)", c.group(0), re.I) and _RESPONDE_EN_EL_CHAT.match(texto, c.end()):
            continue
        return True
    return False


def pedido_con_efecto(mensaje: str) -> str | None:
    texto = mensaje or ""
    if _PREGUNTA_INFORMATIVA.search(texto):
        return None
    for tipo, verbo, objeto in _PEDIDOS_ACCION:
        if _verbo_no_negado(verbo, texto) and (objeto is None or objeto.search(texto)):
            return tipo
    return None


def _es_pregunta(oracion: str) -> bool:
    return "¿" in oracion or oracion.rstrip().endswith("?")


_POR_AUTOR = re.compile(
    r"\**\s+por\s+\**\s*(?!(m[ií]|mi parte|correo|mail|e-?mail|teams|chat|whatsapp|outlook|onedrive|sharepoint|"
    r"la herramienta|el agente|hermes|ia|la ia|inteligencia artificial|el generador)\b)\S",
    re.I,
)


def afirma_hecho(respuesta: str) -> bool:
    texto = respuesta or ""
    fuerte = bool(_MARCA_FUERTE.search(texto))
    if not fuerte:
        if _NO_PUDO.search(texto) or _PROPUESTA.search(texto):
            return False
        oraciones = [o for o in _ORACIONES.split(texto) if o and o.strip()]
        if any(_es_pregunta(o) and not _PREGUNTA_DE_CIERRE.search(o) for o in oraciones):
            return False
    for oracion in _ORACIONES.split(texto):
        if not oracion or not oracion.strip() or _es_pregunta(oracion):
            continue
        for coincidencia in _AFIRMA_HECHO.finditer(oracion):
            if coincidencia.group(1) and _POR_AUTOR.match(oracion, coincidencia.end()):
                continue
            if not _NEGACION.search(oracion[: coincidencia.start()]):
                return True
    return False


_INTENCION = re.compile(
    r"\b(voy a|vamos a|necesito (primero )?(localizar|buscar|revisar|leer|ubicar|abrir|descargar)|primero (voy|debo|"
    r"necesito|reviso|busco|localizo)|a continuaci[óo]n|procedo a|d[ée]jame (buscar|revisar|ver|localizar))\b",
    re.I,
)


def solo_intencion(respuesta: str) -> bool:
    texto = (respuesta or "").strip()
    if not texto or len(texto) > 600 or "MEDIA:" in texto:
        return False
    if _NO_PUDO.search(texto) or _PROPUESTA.search(texto):
        return False
    oraciones = [o for o in _ORACIONES.split(texto) if o and o.strip()]
    if any(_es_pregunta(o) for o in oraciones):
        return False
    return bool(_INTENCION.search(texto))


_NO_ENCONTRADO = re.compile(
    r"\bno (encuentro|encontr[ée]|lo encuentro|la encuentro|existe|est[áa] en tu|aparece|ubico|"
    r"(logro|logr[ée]|pude|puedo|he podido) (encontrar|ubicar|localizar|hallar)|"
    r"(devuelve|trae|muestra|arroja) (ese|esa|ning[uú]n|nada))\b",
    re.I,
)


_ESTADO_O_VERIFICACION_SIN_CONSULTAR = re.compile(
    r"\bya no (existe|est[áa])\b|\bno hay nada que (eliminar|borrar|quitar|retirar|cambiar|editar|modificar)\b|"
    r"\b(lo|la|los|las)\s+(verifiqu[ée]|comprob[ée]|consult[ée]|revis[ée])\b|"
    r"\b(verifiqu[ée]|comprob[ée]|consult[ée])\b[^.\n]{0,60}\b(herramienta|lista|disco)\b",
    re.I,
)


def sin_efecto_declarado(respuesta: str, llamadas: int) -> bool:
    return (
        afirma_hecho(respuesta)
        or solo_intencion(respuesta)
        or (not llamadas and bool(_ESTADO_O_VERIFICACION_SIN_CONSULTAR.search(respuesta or "")))
    )


_PIDE_BUSCAR = re.compile(
    r"\b(b[uú]sca\w*|encu[eé]ntra\w*|ub[ií]ca\w*|revisa\w*|mira\w*|abre\w*|[áa]bre\w*|mu[eé]stra\w*|"
    r"mu[eé]ve\w*|c[aá]mbia\w*|canc[eé]la\w*|elim[ií]na\w*|borra\w*|reprogr\w*)\b",
    re.IGNORECASE,
)


_NEGATIVA_DE_DATOS = re.compile(
    r"\b(no (tengo|tienes|tiene|hay|veo|figura|figuran|aparece|aparecen|existe|existen)|ning[uú]n[oa]?s?)\b"
    r"[^.!?\n]{0,40}?\b(recordatorios?|crons?|tareas? programadas?|programaciones|reuni[oó]n(es)?|eventos?|citas?|"
    r"correos?|mails?|archivos?|documentos?|carpetas?)\b|"
    r"\bning[uú]n[oa]s? (aparece|figura|est[áa]|coincide)\b",
    re.IGNORECASE,
)
_HABLA_DE_CAPACIDAD = re.compile(
    r"\b(acceso|herramientas?|permisos?|capacidad|conect\w*|conexi[óo]n|integraci[óo]n)\b", re.IGNORECASE
)


def negativa_de_datos(respuesta: str) -> bool:
    for oracion in _ORACIONES.split(respuesta or ""):
        if (
            oracion
            and not _es_pregunta(oracion)
            and _NEGATIVA_DE_DATOS.search(oracion)
            and not _HABLA_DE_CAPACIDAD.search(oracion)
        ):
            return True
    return False


def no_encontrado_sin_buscar(respuesta: str) -> bool:
    return bool(_NO_ENCONTRADO.search(respuesta or "")) or negativa_de_datos(respuesta)


_VERBO_CONFIRMA = (
    r"(s[ií]|sip|ok(ey|ay)?|dale|ya|vale|conf[ií]rm\w*|hazlo|h[aá]gale|procede|adelante|de acuerdo|perfecto|"
    r"correcto|exacto|apruebo|aprobad[oa]|env[ií]a(lo|la)?|cr[eé]a(la|lo)?|c[aá]mbia(la|lo)?|mu[eé]ve(la|lo)|"
    r"ag[eé]nda(la|lo)?|c[aá]ncela(la|lo)|b[oó]rra(la|lo)|elim[ií]na(la|lo)|s[uú]be(lo|la)|por favor|porfa|gracias|"
    r"as[ií]|nom[aá]s)"
)
_CONFIRMACION_PURA = re.compile(rf"^\s*{_VERBO_CONFIRMA}([\s,]+{_VERBO_CONFIRMA})*[\s.!,]*$", re.IGNORECASE)
_TIPO_POR_RESPUESTA = (
    (
        "programacion",
        re.compile(r"reuni[oó]n|\bcita\b|\bevento\b|calendario|tarea programada|recordatorio|\bcron\b", re.I),
    ),
    ("correo", re.compile(r"correo|\bmail\b|enviad[oa]|respondid[oa]|reenviad[oa]", re.I)),
    ("subida", re.compile(r"onedrive|sharepoint|subid[oa]|compartid[oa]|enlace", re.I)),
)


_PROPUSO_ACCION = re.compile(
    r"¿[^?¿]{0,80}\b(lo|la|los|las)\s+(env[ií]o|creo|cambio|muevo|agendo|programo|subo|comparto|cancelo|elimino|"
    r"borro|aplico|guardo|actualizo|dejo)\b|¿\s*(confirmas|procedo|lo hago|la hago|te parece|est[aá] bien|"
    r"(aplico|genero|creo|construyo|armo|preparo|elaboro|redacto|ejecuto)\b)|"
    r"vista previa|responde\s+«?s[ií]»?",
    re.IGNORECASE,
)


def confirmacion_narrada(mensaje: str, respuesta: str, anterior: str) -> str | None:
    texto = (mensaje or "").strip()
    if len(texto) > 60 or not _CONFIRMACION_PURA.match(texto) or not _PROPUSO_ACCION.search(anterior or ""):
        return None
    if not afirma_hecho(respuesta):
        return None
    for tipo, patron in _TIPO_POR_RESPUESTA:
        if patron.search(respuesta or ""):
            return tipo
    return "edicion"


_CONFIRMA_AL_INICIO = re.compile(
    r"^\s*(s[ií]|sip|ok(ey|ay)?|dale|vale|conf[ií]rm\w*|hazlo|procede|adelante|de acuerdo|perfecto|correcto|exacto|"
    r"apruebo|aprobad[oa])\b",
    re.IGNORECASE,
)


def confirmacion_con_intencion(mensaje: str, respuesta: str, anterior: str) -> str | None:
    texto = (mensaje or "").strip()
    if len(texto) > 100 or not _CONFIRMA_AL_INICIO.match(texto) or not _PROPUSO_ACCION.search(anterior or ""):
        return None
    return "anuncio" if solo_intencion(respuesta) else None


def accion_narrada(mensaje: str, respuesta: str) -> str | None:
    tipo = pedido_con_efecto(mensaje)
    if tipo == "programacion" and codigos_mostrados(respuesta):
        tipo = None
    if tipo and (afirma_hecho(respuesta) or solo_intencion(respuesta) or _NO_ENCONTRADO.search(respuesta or "")):
        return tipo
    if _PIDE_BUSCAR.search(mensaje or "") and (
        _NO_ENCONTRADO.search(respuesta or "")
        or (negativa_de_datos(respuesta) and _PIDE_BUSCAR.search(_texto_propio_del_usuario(mensaje)))
    ):
        return "busqueda"
    return None


_FUENTES_DE_DATOS = (
    (
        "correos",
        re.compile(r"\b(correos?|mails?|e-?mails?|bandeja|inbox)\b", re.I),
        ("ms365_mail_", "ms365_correo_", "ms365_inbox_"),
        "ms365_mail_list",
    ),
    ("chats de Teams", re.compile(r"\b(chats?|teams)\b", re.I), ("teams_",), "teams_list_chats"),
    (
        "planillas",
        re.compile(
            r"\b(hojas?|planillas?|excel|xlsx|sucursal(es)?)\b",
            re.I,
        ),
        ("hq_leer_documento", "hq_xlsx_"),
        "hq_leer_documento",
    ),
    (
        "reuniones",
        re.compile(r"\b(reuni[oó]n\w*|calendario|agenda|eventos?|citas?)\b", re.I),
        ("ms365_calendar_",),
        "ms365_calendar_list_events",
    ),
    ("archivos", re.compile(r"\b(onedrive|sharepoint)\b", re.I), ("sharepoint_",), "sharepoint_search"),
)
_PIDE_DATOS = re.compile(
    r"\?|\b(qué|cu[aá]l\w*|cu[aá]nt\w*|lista\w*|mu[eé]stra\w*|dame|dime|tengo|hay|revisa\w*|resum\w*|" r"busca\w*)\b",
    re.I,
)
_PIDE_ANOTAR = re.compile(r"\b(recuerda\w*|anota\w*|apunta\w*|guarda\w*|memoriza\w*|ten en cuenta)\b", re.I)
_DATOS_CONCRETOS = re.compile(
    r"^\s*(?:[-*•]\s+|\d+[.)]\s+|\|)\s*\S|\bhace \d+\s*(?:h|min|d)|\b\d{1,2}:\d{2}\b|\btienes \**\d+", re.I | re.M
)
_DATOS_FUERTES = re.compile(r"\bhace \d+\s*(?:h|min|d)|\b\d{1,2}:\d{2}\b|\btienes \**\d+", re.I)
_PIDE_LO_PROPIO = re.compile(
    r"\b(mis|mi|tengo|tienes|sin leer|no le[ií]dos?|nuevos?|recientes?|hoy|ma[ñn]ana|ayer|[uú]ltim\w+|pendientes?|"
    r"esta semana|esta tarde|esta ma[ñn]ana)\b",
    re.I,
)
_HERRAMIENTAS_SIN_DATOS = ("skill_view", "todo", "memory", "clarify")
_PIDE_ACCION = re.compile(
    r"\b(ag[eé]nd(a|ame|ale|as)|program(a|ame|alo|ala)|cr[eé]a(me|lo|la)?|env[ií]a(le|me|lo|la)?|"
    r"resp[oó]nde(le|lo|la)?|reenv[ií]a(le|lo|la)?|mu[eé]ve(la|lo)?|c[aá]mbia(la|lo|le)?|canc[eé]la(la|lo)?|"
    r"elim[ií]na(la|lo)?|b[oó]rra(la|lo)?|s[uú]be(lo|la)?|comp[aá]rte(lo|la|me)?|m[aá]rca(los|las|lo|la)?)\b",
    re.I,
)
_ES_BORRADOR = re.compile(r"\basunto\s*:|¿lo env[ií]o|\bborrador\b|vista previa", re.I)
_LINEA_DE_DATO = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+|\|)\s*(.+)$", re.M)


def _datos_ya_en_la_conversacion(respuesta: str, historial: str) -> bool:
    palabras = {
        p
        for linea in _LINEA_DE_DATO.findall(respuesta or "")
        for p in re.findall(r"\w{5,}", _sin_tildes_runner(linea).lower())
    }
    if not palabras:
        return False
    previo = _sin_tildes_runner(historial or "").lower()
    return sum(1 for p in palabras if p in previo) >= 0.6 * len(palabras)


def consulta_sin_herramienta(mensaje: str, respuesta: str, llamadas: list[str], historial: str = "") -> tuple | None:
    texto = _texto_propio_del_usuario(mensaje)
    planilla = bool(_FUENTES_DE_DATOS[2][1].search(texto) and re.search(r"\d", respuesta or ""))
    if not _PIDE_DATOS.search(texto) or not (_DATOS_CONCRETOS.search(respuesta or "") or planilla):
        return None
    if not _DATOS_FUERTES.search(respuesta or "") and not _PIDE_LO_PROPIO.search(texto) and not planilla:
        return None
    if pedido_con_efecto(mensaje) or _PIDE_ACCION.search(texto) or _ES_BORRADOR.search(respuesta or ""):
        return None
    if _PIDE_ANOTAR.search(texto) and not re.search(r"\?", texto):
        return None
    if any(not str(n).startswith(_HERRAMIENTAS_SIN_DATOS) for n in llamadas):
        return None
    for fuente, patron, prefijos, herramienta in _FUENTES_DE_DATOS:
        if not patron.search(texto):
            continue
        if any(str(n).startswith(prefijos) for n in llamadas):
            return None
        if fuente == "planillas":
            if _cifras_ya_dichas(respuesta, historial):
                return None
        elif _datos_ya_en_la_conversacion(respuesta, historial):
            return None
        return fuente, herramienta
    return None


def _cifras_ya_dichas(respuesta: str, historial: str) -> bool:
    previo = re.sub(r"[*_`]", "", _sin_tildes_runner(historial or "").lower())
    lineas = [
        re.sub(r"[*_`]", "", _sin_tildes_runner(linea).lower()).strip(" -•|")
        for linea in (respuesta or "").splitlines()
        if re.search(r"\d", linea)
    ]
    return bool(lineas) and all(linea and linea in previo for linea in lineas)


_FUENTE_CRON = ("tareas programadas", "hq_schedule_list")
_MENCIONA_CRON = re.compile(r"\b(crons?|tareas? programadas?|tareas? recurrentes?|programaciones)\b", re.I)
_PIDE_DESCRIBIR = re.compile(
    r"\bqu[eé] (hace|hacen|es|son|revisa|revisan|lee|env[ií]a)\b|\bpara qu[eé] (sirve|sirven|es|son)\b|"
    r"\bc[oó]mo (funciona|funcionan|trabaja)\b|\bde qu[eé] (se trata|trata)\b|\bcada cu[aá]nto\b|"
    r"\b(a qu[eé] hora|cu[aá]ndo) (corre|corren|se ejecuta|se ejecutan|se dispara|funciona|sale|llega)\b|"
    r"\bqu[eé] (crons?|tareas? programadas?) tengo\b|\bcu[aá]les son (mis|los|las|tus) (crons?|tareas?)\b|"
    r"\b(expl[ií]ca|descr[ií]be|det[aá]lla)(me|le|les)?\b",
    re.I,
)
_DESCRIBE_UN_CRON = re.compile(
    r"\b(crons?|tareas? programadas?|se ejecuta|se dispara|corre (todos|cada|a las|los)|horario)\b", re.I
)


def _sin_pregunta_final(respuesta: str) -> str:
    oraciones = [o for o in _ORACIONES.split(respuesta or "") if o and o.strip()]
    while oraciones and _es_pregunta(oraciones[-1]):
        oraciones.pop()
    return " ".join(oraciones).strip()


def cron_descrito_sin_consultar(mensaje: str, respuesta: str, llamadas: list[str]) -> tuple | None:
    texto = _texto_propio_del_usuario(mensaje)
    if not _MENCIONA_CRON.search(texto) or not _PIDE_DESCRIBIR.search(texto):
        return None
    if pedido_con_efecto(texto) or _PIDE_ACCION.search(texto):
        return None
    if any(str(n).startswith("hq_schedule_") for n in llamadas):
        return None
    descripcion = _sin_pregunta_final(respuesta)
    if len(descripcion) < 120 or not _DESCRIBE_UN_CRON.search(descripcion):
        return None
    return _FUENTE_CRON


def consulto_la_fuente(fuente: str, llamadas: list[str]) -> bool:
    if fuente == _FUENTE_CRON[0]:
        return any(str(n).startswith("hq_schedule_") for n in llamadas)
    prefijos = next((p for f, _, p, _ in _FUENTES_DE_DATOS if f == fuente), ())
    return any(str(n).startswith(prefijos) for n in llamadas) if prefijos else False


def nota_consulta_sin_herramienta(fuente: str, herramienta: str) -> str:
    return (
        f"[Nota del sistema] Tu respuesta anterior dio datos de {fuente} (nombres, horas o cantidades) sin consultar "
        f"ninguna herramienta en esta tarea, así que esos datos no son reales. Consulta ahora con {herramienta} y "
        "responde solo con lo que devuelva; si no puedes consultarlo, dilo." + _SIN_MENCIONAR_REVISION
    )


def aviso_consulta_sin_herramienta(fuente: str) -> str:
    return f"⚠️ No consulté tus {fuente} en esta respuesta, así que no puedo darte esos datos. Pídemelo de nuevo y los reviso."


def _texto_del_historial(historial) -> str:
    return "\n".join(
        str(m.get("content") or "")
        for m in historial or []
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )


def _respuesta_anterior(mensajes: list, inicio: int) -> str:
    for m in reversed(mensajes[: max(inicio, 0)]):
        if isinstance(m, dict) and m.get("role") == "assistant" and str(m.get("content") or "").strip():
            return str(m["content"])
    return ""


_NOTA_CALENDARIO = (
    " Si es una reunión: búscala con ms365_calendar_list_events y usa su ref (E1, E2…) como event_id; no le pidas "
    "el id al usuario."
)


_NOTA_BUSQUEDA = (
    "[Nota del sistema] Tu respuesta anterior dijo que algo no existe o que no lo encontraste, pero en esta tarea no "
    "consultaste nada, así que no lo sabes. Consúltalo ahora con la herramienta que corresponde (hq_schedule_list "
    "para recordatorios y crons, ms365_calendar_list_events para reuniones, ms365_mail_search para correos, "
    "sharepoint_search para archivos) y responde con lo que devuelva. Si no tienes una herramienta para "
    "consultarlo, dilo."
)
_AVISO_NEGATIVA_SIN_CONSULTA = (
    "⚠️ No consulté tus datos en esta respuesta, así que no puedo asegurarte que no exista. Pídemelo de nuevo y lo "
    "reviso."
)


_VISTA_A_MANO_CANCELA = re.compile(r"cancelaci[óo]n|\b(elimin|cancel|borr)\w*", re.I)
_VISTA_A_MANO_CAMBIA = re.compile(
    r"vista previa del cambio|\b(mov|muev|cambi|modific|reprogram|ampl[íi]|agreg)\w*", re.I
)


def _nota_vista_calendario(respuesta: str) -> str:
    if _VISTA_A_MANO_CANCELA.search(respuesta or ""):
        herramienta = "ms365_calendar_delete_event"
    elif _VISTA_A_MANO_CAMBIA.search(respuesta or ""):
        herramienta = "ms365_calendar_update_event"
    else:
        return _NOTA_VISTA_CALENDARIO
    return (
        "[Nota del sistema] Escribiste a mano la vista previa, así que el servidor no la registró y el «sí» del usuario "
        f"no podría ejecutarla. Llama AHORA {herramienta} con event_id = la ref (E1, E2…) de la reunión en el listado "
        'de este turno (si no la tienes, búscala con ms365_calendar_list_events(buscar="<título>")), SIN '
        "aprobado_por_el_usuario. No escribas la vista previa: la muestra la plataforma." + _SIN_MENCIONAR_REVISION
    )


def nota_accion_narrada(tipo: str, respuesta: str = "", lecturas: list | None = None) -> str:
    if tipo == "vista_calendario":
        return _nota_vista_calendario(respuesta)
    if tipo.startswith("vista_envio_"):
        return _nota_vista_envio(tipo.removeprefix("vista_envio_"))
    if tipo == "correo_incoherente":
        return _nota_correo_incoherente(respuesta)
    if tipo == "busqueda":
        return _NOTA_BUSQUEDA + _SIN_MENCIONAR_REVISION
    if tipo == "programacion" and _CALENDARIO_EN_RESPUESTA.search(respuesta or ""):
        return nota_accion_narrada(tipo, lecturas=lecturas) + _NOTA_CALENDARIO
    if tipo == "anuncio":
        return (
            "[Nota del sistema] El usuario confirmó lo que propusiste y tu respuesta solo anunció que lo harías, sin "
            "llamar ninguna herramienta, así que no ocurrió nada. Llama ahora la herramienta que corresponde, sin "
            "describir lo que vas a hacer." + _SIN_MENCIONAR_REVISION
        )
    hecho = _HECHO_PEDIDO.get(tipo, _HECHO_PEDIDO["edicion"])[0]
    if lecturas:
        motivo = (
            f"pero solo usaste herramientas de lectura ({', '.join(lecturas)}): no se generó, guardó, subió ni envió "
            "nada, y adjuntar un archivo que ya existía no es una versión nueva"
        )
    else:
        motivo = "pero no llamaste ninguna herramienta, así que no ocurrió nada"
    return (
        f"[Nota del sistema] Tu respuesta anterior afirmó haber hecho lo pedido ({hecho}) {motivo}. Hazlo ahora con "
        "la herramienta correspondiente, o di claramente que no pudiste." + _SIN_MENCIONAR_REVISION
    )


_ENLACE_EN_RESPUESTA = re.compile(r"enlace|link|compart|1drv\.ms|sharepoint\.com", re.IGNORECASE)


_PIDE_UNA_SKILL = re.compile(r"\bskills?\b", re.I)


def aviso_accion_no_hecha(tipo: str, respuesta: str = "", solo_lectura: bool = False, mensaje: str = "") -> str:
    objeto = _HECHO_PEDIDO.get(tipo, _HECHO_PEDIDO["edicion"])[1]
    if tipo == "creacion" and _PIDE_UNA_SKILL.search(mensaje or ""):
        objeto = "la creación de la skill"
    if tipo == "programacion" and _CALENDARIO_EN_RESPUESTA.search(respuesta or ""):
        objeto = "el cambio en la reunión"
    elif tipo == "subida" and _ENLACE_EN_RESPUESTA.search(respuesta or ""):
        objeto = "el enlace para compartir (no se generó ningún enlace)"
    if solo_lectura:
        return (
            f"No pude completar {objeto}: solo leí datos y no se generó, guardó ni envió nada nuevo, así que no "
            "cambió nada. Vuelve a pedírmelo."
        )
    return f"No pude completar {objeto}: no se ejecutó ninguna herramienta, así que no cambió nada. Vuelve a pedírmelo."


_HERRAMIENTAS_SOLO_LECTURA = frozenset(
    {
        "read_file",
        "search_files",
        "skill_view",
        "skills_list",
        "session_search",
        "web_search",
        "web_extract",
        "vision_analyze",
        "clarify",
        "hq_leer_documento",
        "hq_leer_varios",
        "hq_buscar_varios",
        "hq_html_capturar",
        "hq_skills_ver",
        "hq_schedule_list",
        "hq_schedule_runs",
        "hq_list_agents",
        "ms365_calendar_get_event",
        "ms365_calendar_list_events",
        "ms365_find_person",
        "ms365_inbox_estado",
        "ms365_mail_attachments",
        "ms365_mail_folders",
        "ms365_mail_get",
        "ms365_mail_get_many",
        "ms365_mail_list",
        "ms365_mail_search",
        "sharepoint_download_file",
        "sharepoint_download_folder",
        "sharepoint_get_file",
        "sharepoint_list_drives",
        "sharepoint_list_files",
        "sharepoint_read_file",
        "sharepoint_search",
        "teams_get_chat_messages",
        "teams_list_chats",
        "teams_list_teams",
    }
)
_ENTREGABLE = (
    r"dashboards?|tableros?|archivos?|excel|planillas?|html|informes?|reportes?|presentaci[óo]n(es)?|documentos?|"
    r"pdf|word|docx|xlsx|pptx|versi[óo]n|v\d+"
)
_PRODUJO = (
    r"gener[ée]|cre[ée]|redise[ñn][ée]|actualic[ée]|modifiqu[ée]|guard[ée]|export[ée]|constru[íi]|hice|rehice|"
    r"arm[ée]|edit[ée]|ajust[ée]"
)
_PRODUCIDO = r"generad|cread|redise[ñn]ad|actualizad|modificad|guardad|exportad|construid|editad|ajustad|rehech|mejorad"
_AFIRMA_ARCHIVO = re.compile(
    rf"\b({_PRODUJO})\s+(el|la|los|las|un|una|tu|su|otro|otra|nuevo|nueva|de nuevo)?\s*(nuev[oa]\s+)?\**\s*({_ENTREGABLE})\b|"
    rf"\b({_ENTREGABLE})\**\s+({_PRODUCIDO})[oa]s?\b|"
    r"se entrega como adjunto|\bruta del? (html|archivo|excel|informe|dashboard)\b|"
    r"\bqued[óa] en (el directorio de salida|outputs)|^\s*MEDIA:",
    re.IGNORECASE | re.MULTILINE,
)
_TIPOS_CON_ARCHIVO = ("creacion", "edicion")
_TIPOS_SOLO_LECTURA = ("creacion", "edicion", "subida", "correo", "programacion")


def afirma_archivo(respuesta: str) -> bool:
    return bool(_AFIRMA_ARCHIVO.search(respuesta or ""))


def lecturas_del_turno(mensajes: list, inicio: int) -> list[str] | None:
    nombres = []
    for m in mensajes[inicio + 1 :]:
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        for llamada in m.get("tool_calls") or []:
            funcion = llamada.get("function") if isinstance(llamada, dict) else None
            nombre = funcion.get("name") if isinstance(funcion, dict) else None
            if nombre not in _HERRAMIENTAS_SOLO_LECTURA:
                return None
            nombres.append(nombre)
    return sorted(set(nombres)) or None


def accion_narrada_tras_lecturas(mensaje: str, respuesta: str) -> str | None:
    tipo = pedido_con_efecto(mensaje)
    if tipo not in _TIPOS_SOLO_LECTURA or not afirma_hecho(respuesta):
        return None
    if tipo in _TIPOS_CON_ARCHIVO and not afirma_archivo(respuesta):
        return None
    return tipo


def _llamadas_con_efecto_desde(mensajes: list, inicio: int) -> int:
    return sum(
        1
        for m in mensajes[inicio + 1 :]
        if isinstance(m, dict) and m.get("role") == "assistant"
        for llamada in m.get("tool_calls") or []
        if ((llamada.get("function") or {}) if isinstance(llamada, dict) else {}).get("name")
        not in _HERRAMIENTAS_SOLO_LECTURA
    )


_RECHAZO_DE_CAPACIDAD = re.compile(
    r"no (?:esta|se encuentra) (?:disponible|en mi lista)|no (?:la|lo) tengo|no tengo (?:la |una |ninguna |esa )?"
    r"herramienta|no (?:existe|hay) (?:otra|ninguna|una|esa)? ?herramienta|no existe otra que|no cuento con (?:la |una |"
    r"ninguna )?herramienta|no tengo (?:como|forma de|manera de) (?:editar|modificar|cambiar|crear|generar|subir)|"
    r"no (?:incluye|tiene|ofrece|permite) (?:la |una |esa |ninguna )?(?:accion|opcion|funcion|operacion)"
)
_NOMBRE_DE_HERRAMIENTA = re.compile(r"\b(?:hq|ms365|sharepoint|teams)_[a-z0-9_]+\b")
_EDICION_PEDIDA = re.compile(r"modific|edit|cambi|corrig|arregl|actualiz|reemplaz|ajust|agreg|quit|elimin|borr")
_HERRAMIENTAS_POR_PEDIDO = (
    (
        r"presentacion|diapositiva|lamina|slide|pptx|powerpoint|deck",
        ("hq_pptx_editar",),
        ("hq_pptx_generate",),
    ),
    (
        r"\bword\b|docx|documento|procedimiento|politica",
        ("hq_docx_editar",),
        ("hq_docx_generate",),
    ),
    (r"excel|xlsx|planilla|hoja de c[aá]lculo|matriz", ("hq_xlsx_editar",), ("hq_xlsx_generate",)),
    (
        r"onedrive|sharepoint|\bsub(?:e|ir|elo|ela|irlo|irla)\b",
        ("sharepoint_upload_file",),
        ("sharepoint_upload_file",),
    ),
    (r"\bzip\b|comprim|descomprim", ("execute_code", "terminal"), ("execute_code", "terminal")),
)


def herramientas_del_pedido(mensaje: str, registradas, tema: str | None = None) -> list[str]:
    texto = _sin_tildes_runner(mensaje or "").lower()
    asunto = _sin_tildes_runner(tema).lower() if tema is not None else texto
    edicion = bool(_EDICION_PEDIDA.search(texto))
    candidatas = [
        nombre
        for patron, editar, crear in _HERRAMIENTAS_POR_PEDIDO
        if re.search(patron, asunto)
        for nombre in (editar if edicion else crear)
    ]
    return [n for n in dict.fromkeys(candidatas) if n in (registradas or ())]


def herramientas_negadas_disponibles(mensaje: str, respuesta: str, registradas) -> list[str]:
    if not registradas or not respuesta:
        return []
    disponibles = []
    for frase in re.split(r"(?<=[.!?\n])\s+", respuesta):
        plana = _sin_tildes_runner(frase).lower()
        if not _RECHAZO_DE_CAPACIDAD.search(plana):
            continue
        nombres = _NOMBRE_DE_HERRAMIENTA.findall(frase)
        if not nombres and "herramienta" not in plana and "tool" not in plana:
            continue
        disponibles.extend(n for n in nombres if n in registradas)
        disponibles.extend(herramientas_del_pedido(mensaje, registradas, tema=frase))
    return list(dict.fromkeys(disponibles))


def nota_rechazo_falso(disponibles: list[str]) -> str:
    return (
        "[Nota del sistema] Dijiste que no tienes una herramienta para esto, pero en esta tarea sí tienes: "
        f"{', '.join(disponibles)}. Úsala ahora para lo que pidió el usuario; si devuelve un error, infórmalo tal cual "
        "en palabras simples." + _SIN_MENCIONAR_REVISION
    )


AVISO_RECHAZO_FALSO = (
    "No alcancé a hacerlo en este intento, aunque la herramienta para hacerlo sí está disponible. Escribe «reintenta» "
    "y lo hago de nuevo."
)


def _herramientas_registradas(agente) -> set[str]:
    nombres = getattr(agente, "valid_tool_names", None)
    if nombres:
        return set(nombres)
    return {
        str(((t or {}).get("function") or {}).get("name") or "") for t in (getattr(agente, "tools", None) or [])
    } - {""}


def _nombres_llamados_desde(mensajes: list, inicio: int) -> list[str]:
    return [
        str(((llamada.get("function") or {}) if isinstance(llamada, dict) else {}).get("name") or "")
        for m in mensajes[inicio + 1 :]
        if isinstance(m, dict) and m.get("role") == "assistant"
        for llamada in m.get("tool_calls") or []
    ]


def _indice_ultimo_usuario(mensajes: list) -> int:
    return max((i for i, m in enumerate(mensajes) if isinstance(m, dict) and m.get("role") == "user"), default=-1)


def _llamadas_desde(mensajes: list, inicio: int) -> int:
    return sum(
        len(m.get("tool_calls") or [])
        for m in mensajes[inicio + 1 :]
        if isinstance(m, dict) and m.get("role") == "assistant"
    )


_EFECTOS_POR_TIPO = {
    "subida": frozenset({"sharepoint_upload_file", "sharepoint_share_file"}),
    "correo": frozenset(
        {
            "ms365_mail_send",
            "ms365_mail_reply",
            "ms365_mail_forward",
            "teams_send_chat_message",
            "teams_send_to_person",
            "hq_direct_message",
            "hq_delegate_task",
            "delegate_task",
        }
    ),
    "calendario": frozenset(
        {
            "ms365_calendar_create_event",
            "ms365_calendar_update_event",
            "ms365_calendar_delete_event",
            "ms365_calendar_respond_event",
        }
    ),
    "programacion": frozenset(
        {"hq_schedule_request", "hq_schedule_approve", "hq_schedule_delete", "hq_schedule_pause", "hq_schedule_resume"}
    ),
    "marcado": frozenset({"ms365_mail_mark_read", "ms365_correo_sync"}),
    "preferencia": frozenset({"memory", "ms365_correo_sync", "hq_solicitar_capacidad"}),
}
_AFIRMA_MARCADO = re.compile(
    r"\b(marqu[ée]|he marcado|hemos marcado|se marcaron|(qued(aron|ó|an|a)|fueron) marcad[oa]s?)\b", re.IGNORECASE
)
_LEIDO = re.compile(r"\ble[ií]d[oa]s?\b", re.IGNORECASE)
_OBJETO_PREFERENCIA = re.compile(
    r"\bpreferencias?\b|\bregla\b|para (las |tus )?pr[óo]ximas (corridas|sesiones)|\ben (mi |tu |la )?memoria\b", re.I
)
_AFIRMA_GUARDADO = re.compile(
    r"\b(la |lo )?(guard[ée]|qued[óo] (guardad|anotad|registrad)|dej[ée] (guardad|anotad)|anot[ée]|registr[ée])\w*|"
    r"\bguardad[oa]s?\b|\bqueda\s+(anotad|guardad|registrad)\w*",
    re.IGNORECASE,
)
_PENDIENTE_CUENTA = frozenset({"programacion"})
_LUGAR_REMOTO = re.compile(r"onedrive|sharepoint|\bcarpeta\b|biblioteca|[a-z]:\\", re.I)
_VERBO_SUBIDA = re.compile(
    r"\b(guardé|subí|he (guardado|subido)|se (guard|sub)ó)\b|"
    r"\b(est[áa]n?|qued[óa]|quedaron|fue|fueron|ha sido|han sido)\s+(\w+\s+y\s+)?(guardad|subid|almacenad)[oa]s?\b|"
    r"^\W*(guardad|subid)[oa]s?\s+en\b|✅\s*(guardad|subid)[oa]",
    re.I,
)
_VERBO_SUBIDA_CON_LUGAR = re.compile(
    r"\b(dejé|quedó|quedaron|he dejado)\b[^.\n]{0,40}\ben (tu|la|el|su|mi)\s+(carpeta|onedrive|sharepoint|biblioteca|sitio)",
    re.I,
)
_LUGAR_LOCAL = re.compile(
    r"outputs/|sharepoint_downloads|\bwork/|\bworkspace\b|espacio de trabajo|`[^`\s]+\.[a-z0-9]{2,5}`", re.I
)
_PIDE_O_PROPONE = re.compile(
    r"^\W*si\b|\bsi\s+(la|el|los|las|est|hay|existe|lo|me)\b|\bpuedo\b|\bpodr[íi]a|\bquieres\b|\bprefieres\b|"
    r"\b(ind[íi]came|dime|conf[íi]rmame|env[íi]ame|p[áa]same|comp[áa]rteme|av[íi]same|quedo a la espera)\b|"
    r"\bcuando\b|\bva(s|mos)? a\b|\bvoy a\b|y/o|\bque pidas\b|quedar[áa]|se (guardar|subir|enviar)[áa]|"
    r"\bpara (dejarlo|dejarla|que)\b|^\W*(buscar|revisar|subir|guardar|enviar|entrega(ble)?)\b(?!\W*:)|"
    r"^\W*(d[óo]nde|qu[ée]|cu[áa]l(es)?|c[óo]mo)\b",
    re.I,
)
_HABLA_DE_SKILL = re.compile(r"\bskills?\b", re.I)
_VERIFICA_ENVIO = re.compile(r"verific|lo confirmo|confirm[ée]|bandeja de (salida|enviados)|\ben enviados\b", re.I)
_LECTURAS_CORREO = frozenset({"ms365_mail_search", "ms365_mail_list", "ms365_mail_get", "ms365_mail_get_many"})
_OBJETO_DE_EVENTO = re.compile(r"reuni[óo]n|cancelaci[óo]n|invitaci[óo]n|evento", re.I)
_PROPONE_ANTES = re.compile(
    r"te propongo|propuesta|antes de generar|si (me )?confirmas|¿\s*procedo|¿\s*te parece|¿\s*confirmas", re.I
)
_PIDE_ESTADO = re.compile(
    r"qu[ée] pas[óo]|expl[íi]came|no hagas nada|c[óo]mo va|en qu[ée] (va|est[áa])|qu[ée] hiciste", re.I
)
_OBJETO_CORREO = re.compile(r"correo|\bmail\b|e-?mail|mensaje|respuesta|\bteams\b|\bchat\b|destinatari|@", re.I)
_VERBO_CORREO = re.compile(
    r"\b(envié|reenvié|respondí|contesté|le escribí|les escribí|he (enviado|reenviado|respondido|"
    r"contestado|escrito))\b|"
    r"^\W*(env[íi]o|reenv[íi]o|mando)\s+(el|tu|la|ese|este|esa|esta)\s+(mensaje|correo|respuesta)\b",
    re.I,
)
_IMPERSONAL_CORREO = re.compile(r"\bse (envi|reenvi|respondi)ó\b", re.I)
_ENVIO_A_TERCERO = re.compile(
    r"\b(envié|reenvié|mandé|he (enviado|reenviado|mandado))\b[^.\n]{0,60}\b(a|para)\s+(?!ti\b|usted\b)"
    r"(\w+@|[A-ZÁÉÍÓÚÑ]|gerencia|todos|el equipo|los|las|la|el|tu jefe|su)",
)
_NOMINAL_CORREO = re.compile(r"\b(correo|mail|mensaje|respuesta)s?\s+(enviad|reenviad|respondid)[oa]s?\b", re.I)
_OBJETO_CALENDARIO = re.compile(r"reuni[óo]n|\bcita\b|\bevento\b|calendario|invitaci[óo]n", re.I)
_VERBO_CALENDARIO = re.compile(
    r"\b(agendé|reprogramé|cancelé|moví|invité|acepté|rechacé|creé|actualicé|eliminé|"
    r"he (agendado|reprogramado|cancelado|movido|creado|invitado))\b",
    re.I,
)
_IMPERSONAL_CALENDARIO = re.compile(r"\bse (agend|reprogram|cancel|movi|cre)ó\b", re.I)
_NOMINAL_CALENDARIO = re.compile(
    r"\b(reuni[óo]n|evento|cita|invitaci[óo]n)(es)?\b[^.\n]{0,40}\b(agendad|cread|movid|reprogramad|cancelad|actualizad|"
    r"enviad)[oa]s?\b",
    re.I,
)
_VERBO_PROGRAMACION = re.compile(
    r"\b(programé|he programado|se programó)\b|"
    r"\b(tarea|programaci[oó]n)\b[^.\n]{0,40}\b(qued[óo]|fue|ha sido|est[áa])\s+(eliminad|borrad|pausad|reanudad|desactivad|activad)[oa]\b|"
    r"\b(tarea|programaci[oó]n)\s+(eliminad|borrad|pausad|reanudad|desactivad)[oa]\b|\bestado:\s*(eliminad|pausad|reanudad)[oa]\b|"
    r"\b(creé|configuré|dejé|activé|pausé|reanudé|eliminé)\s+(el|la|un|una|tu)\s+"
    r"(recordatorio|alerta|tarea programada|cron|aviso)\b|"
    r"\bte (avisar[ée]|recordar[ée]|escribir[ée]) (cada|todos|todas|el|la|ma[ñn]ana|a las|en|diariamente)\b",
    re.I,
)
_NOMINAL_PROGRAMACION = re.compile(
    r"\b(recordatorio|tarea programada|alerta|cron)s?\b[^.\n]{0,40}\b(cread|programad|activad|configurad|eliminad|"
    r"pausad|reanudad)[oa]s?\b",
    re.I,
)
_CONFIRMA = re.compile(r"\blisto\b|✅|\bhecho\b|correctamente|exitosamente|con [ée]xito", re.I)
_CITADO = re.compile(r"«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"")
_LINEA_CITA_O_TABLA = re.compile(r"(?m)^\s*[|>].*$")
_REFIERE_AL_PASADO = re.compile(
    r"\b(antes|anterior(es|mente)?|ayer|anteayer|hace \d+|previ[oa]s?|previamente|la vez pasada|"
    r"el (lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo) pasado|la semana pasada|el mes pasado|"
    r"otra conversaci[óo]n|hace un momento|hace (unos )?minutos|reci[ée]n)\b|"
    r"\b(que|donde) (te |le |les )?(envié|reenvié|respondí|agendé|guardé|subí|programé|creé)\b",
    re.I,
)
_URL_REMOTA = re.compile(
    r"https?://[^\s)\]>»\"'`]*(?:sharepoint\.com|1drv\.ms|onedrive\.live\.com)[^\s)\]>»\"'`]*", re.I
)
_LINEA_MEDIA = re.compile(r"(?im)^[ \t*_`]*MEDIA:[ \t]*(\S.*?)[ \t*_`]*$")
_MARCA_MEDIA = re.compile(r"(?i)MEDIA:\S*")
_RUTA_DE_PC = re.compile(r"^[a-z]:\\", re.I)
_EXISTENCIA = re.compile(r"\bya exist\w*|\best[áa] (guardad|subid|en (la|tu))|\bse encuentra", re.I)
_LECTURAS_REMOTAS = frozenset({"sharepoint_list_files", "sharepoint_search", "sharepoint_get_file"})
_PREGUNTA_UBICACION = re.compile(
    r"(d[oó]nde|en qu[eé] carpeta)\b.{0,50}\b(qued[oó]|est[aá]|guard|dej|sub|pusiste)|"
    r"\bno (lo|la|los|las) (veo|encuentro|hallo)\b|\bno (me )?aparece\b",
    re.I,
)
_OBJETO_ARCHIVO = re.compile(
    r"carpeta|onedrive|sharepoint|guard|sub[ií]|subiste|"
    r"(archivo|informe|documento|excel|presentaci|reporte|dashboard).{0,30}\b(crear|creaste|generaste|hiciste)\b|"
    r"\b(crear|creaste|generaste|hiciste)\b",
    re.I,
)
_VERIFICAN_UBICACION = frozenset(
    {
        "sharepoint_search",
        "sharepoint_list_files",
        "sharepoint_get_file",
        "sharepoint_upload_file",
        "sharepoint_share_file",
    }
)
_PIDE_GUARDAR = re.compile(r"\b(gu[áa]rd|sub[íie]|s[úu]b)\w*", re.I)
_MOTIVO_VERIFICACION = {
    "html_roto": (
        "al abrir esa página en un navegador de prueba su JavaScript falla, así que el usuario la vería vacía, "
        "incompleta o sin responder"
    ),
    "html_clic": "al hacer clic en esa página su JavaScript falla y esa parte no responde",
    "html_sin_efecto": (
        "en un navegador de prueba la mayoría de los elementos con onclick no cambian nada al hacer clic (el "
        "explorador, las carpetas o los botones no responden). Suele pasar cuando varios bloques comparten una función "
        "global (p. ej. window.openNode) que se pisa con la del último bloque creado: haz que cada onclick use el "
        "contenedor o el estado que le corresponde y comprueba que un clic cambia lo que se ve"
    ),
    "html_no_corregido": (
        "dices que el HTML quedó corregido o válido, pero lo abrí en un navegador de prueba y su JavaScript sigue "
        "fallando: tu cambio no lo arregló. No digas que está corregido ni culpes a la herramienta"
    ),
    "subida": "no hubo ninguna subida exitosa a OneDrive o SharePoint (sharepoint_upload_file) en esta tarea",
    "subida_pendiente": "solo se generó la vista previa de la subida: espera la aprobación del usuario, no se subió nada",
    "correo": "no se ejecutó ningún envío de correo o mensaje en esta tarea",
    "correo_pendiente": "solo se generó la vista previa: el envío espera la aprobación del usuario, no se envió nada",
    "calendario": "no se creó ni cambió ninguna reunión en esta tarea",
    "calendario_pendiente": "solo se generó la vista previa de la reunión: espera la aprobación del usuario",
    "programacion": "no se creó ni cambió ninguna tarea programada en esta tarea",
    "marcado": (
        "no se marcó ningún correo como leído en esta tarea (no hubo ms365_mail_mark_read exitoso): si el usuario "
        "lo pidió, márcalos ahora con ms365_mail_mark_read usando los ids reales; si no, di que no se marcaron"
    ),
    "preferencia": (
        "no quedó guardada ninguna preferencia en esta tarea (la herramienta no se usó o falló): no digas que quedó "
        "guardada; explica qué sí quedó hecho"
    ),
    "archivo": "no se ejecutó ninguna herramienta que genere o modifique archivos en esta tarea",
    "enlace": "ese enlace no salió de ninguna herramienta ni del usuario: es inventado",
    "adjunto": "ese archivo no existe en tu workspace, así que no se puede adjuntar",
    "ubicacion": (
        "el usuario preguntó dónde está el archivo y no lo revisaste: búscalo con sharepoint_search o "
        "sharepoint_list_files en la carpeta que pidió, y responde con la ubicación y el enlace reales; si no está, "
        "dilo"
    ),
    "entrega_sin_archivo": (
        "el usuario pidió que le entregues un archivo y no lo adjuntaste ni ejecutaste ninguna herramienta. Si el "
        "archivo existe en tu workspace, adjúntalo con su línea MEDIA:; si no existe, créalo ahora (un .zip con "
        "execute_code y el módulo zipfile, dentro de outputs/) y adjúntalo; si no puedes, dilo claramente sin "
        "describirlo como si existiera"
    ),
    "adjunto_pc": (
        "esa es una ruta del PC del usuario: no puedes escribir ahí ni adjuntarla; súbelo a su OneDrive con "
        "sharepoint_upload_file y adjunta el archivo del workspace"
    ),
    "codigo": (
        "ese código no salió de ninguna herramienta en esta tarea: es inventado y no sirve. Los códigos los genera "
        "el servidor: para eliminar un cron llama hq_schedule_delete (si está aprobado, su respuesta trae el texto "
        "exacto «eliminar …»); hq_schedule_list da los crons con sus datos reales y el approval_code de los "
        "pendientes (no crees una solicitud nueva solo para obtener un código). Muestra solo el código y los datos "
        "que devuelva la herramienta"
    ),
    "excel_inventado": (
        "el Excel que generaste tiene filas que no están en los datos que escribió el usuario: genera de nuevo el "
        "Excel con exactamente sus filas y cifras, sin agregar, separar ni extrapolar filas"
    ),
    "informe_copiado": (
        "ese informe de correos no salió de ms365_correo_sync en esta tarea (es una copia de uno anterior, con "
        "datos viejos): llama ahora ventana, plan y commit y entrega la tabla que devuelva commit"
    ),
    "subida_otra_carpeta": (
        "el usuario pidió guardar el archivo en una carpeta y ninguna subida con éxito quedó ahí: no digas que quedó "
        "en la ruta pedida. Di exactamente dónde quedó cada copia (la carpeta que devolvió sharepoint_upload_file) y "
        "qué ruta no se pudo usar y por qué"
    ),
    "renombre": (
        "no tienes ninguna herramienta para renombrar ni mover archivos de OneDrive o SharePoint: sharepoint_upload_file "
        "con otro nombre crea una COPIA nueva y el archivo anterior sigue ahí con su nombre. No digas que lo renombraste: "
        "di que quedó una copia con el nombre nuevo (carpeta que devolvió sharepoint_upload_file), que el archivo "
        "anterior sigue en la carpeta y que puede borrarlo o renombrarlo él en OneDrive"
    ),
    "funciones_inexistentes": (
        "tu respuesta promete funciones que el HTML que entregas no contiene (lo abrí y no aparecen en el archivo). "
        "Agrégalas de verdad al HTML con patch o write_file, o quítalas de la respuesta y di claramente qué no incluye"
    ),
}
_AVISO_VERIFICACION = {
    "entrega_sin_archivo": "No te adjunté ningún archivo: en esta respuesta no se generó ni se adjuntó nada.",
    "subida": "No se guardó ni subió ningún archivo a OneDrive o SharePoint.",
    "subida_pendiente": "El archivo no se guardó ni subió todavía: la subida espera tu aprobación.",
    "correo": "No se envió ningún correo ni mensaje.",
    "correo_pendiente": "El correo o mensaje no se envió: falta tu aprobación.",
    "calendario": "No se creó ni cambió ninguna reunión.",
    "calendario_pendiente": "La reunión no se creó ni cambió: falta tu aprobación.",
    "programacion": "No se creó ni cambió ninguna tarea programada.",
    "marcado": "No se marcó ningún correo como leído.",
    "preferencia": "La preferencia no quedó guardada.",
    "archivo": "No se generó ni modificó ningún archivo.",
    "enlace": "Quité un enlace que no salió de OneDrive ni de SharePoint.",
    "ubicacion": "No revisé OneDrive ni SharePoint para confirmar dónde quedó el archivo.",
    "codigo": "Quité un código de confirmación que no salió del servidor: no lo uses.",
    "excel_inventado": (
        "El Excel no coincidía con los datos que me diste (tenía filas que no pediste), así que no te lo adjunto."
    ),
    "informe_copiado": "No revisé tu bandeja en esta respuesta, así que no te entrego un informe copiado del anterior.",
    "funciones_inexistentes": "El HTML no contiene estas funciones que mencioné: {funciones}; las quité de la respuesta.",
    "html_roto": (
        "No te entrego «{nombre}»: lo abrí en un navegador de prueba y su JavaScript falla ({resumen}), así que se "
        "vería vacía, incompleta o sin responder."
    ),
    "html_no_corregido": (
        "«{nombre}» no quedó corregido: lo abrí en un navegador de prueba y su JavaScript sigue fallando ({resumen})."
    ),
    "html_sin_efecto": (
        "Ojo: abrí «{nombre}» en un navegador de prueba y {resumen}, así que esa parte no responde al usuario."
    ),
    "html_clic": (
        "Ojo: abrí «{nombre}» en un navegador de prueba y al hacer clic en {elemento} su JavaScript falla ({resumen}); "
        "esa parte no responde."
    ),
    "subida_otra_carpeta": "El archivo no quedó en la carpeta que pediste: revisa la ruta antes de usarlo.",
    "renombre": (
        "El archivo no se renombró: se subió una copia con el nombre nuevo y el anterior sigue en la carpeta. Puedes "
        "borrar o renombrar el anterior desde OneDrive."
    ),
    "codigo_sin_consulta": (
        "No consulté tus tareas programadas, así que quité el código y los datos que había escrito: no salieron "
        "del servidor."
    ),
}
_CODIGO_MOSTRADO = re.compile(
    r"(?i:\b(?:aprobar|eliminar)\b)[\s`*_«»\"'“”:→=>-]*([A-Z0-9]{4,12})\b(?![-_/.]\w)|"
    r"(?i:\bc[óo]digos?\s+de\s+(?:confirmaci[óo]n|aprobaci[óo]n|eliminaci[óo]n|borrado)(?:\s+(?:es|ser[íi]a))?)"
    r"[\s`*_«»\"'“”:→=>-]*([A-Z0-9]{4,12})\b(?![-_/.]\w)"
)
_CODIGOS_DE_EJEMPLO = frozenset({"CODE", "CODIGO", "CODIGOS", "XXXX", "XXXXX", "XXXXXX", "ABC123", "ABCDEF", "CODE123"})
_LARGO_CODIGO = 6
_HERRAMIENTAS_DE_CODIGO = frozenset(
    {"hq_schedule_request", "hq_schedule_delete", "hq_schedule_list", "hq_schedule_approve"}
)


_NUMERO_EN_TEXTO = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w.,]*\w)")
_FILA_RESUMEN = re.compile(r"^(total(es)?|subtotal|promedio|suma|general|resumen|variacion|acumulado)\b", re.I)
_TABLA_INFORME_CORREO = re.compile(r"\|\s*ID\s*\|\s*Edad\s*\|\s*Asunto\s*\|", re.I)
_REFERENCIA_A_UN_ARCHIVO = re.compile(
    r"adjunt|uploads/|\.(xlsx?|xlsm|csv|pdf|docx)\b|sharepoint|onedrive|"
    r"\b(el|la|los|las|este|esta|ese|esa|del|de la|mi|tu) (excel|planillas?|archivos?|tablas?|hojas?)\b",
    re.I,
)


def _sin_tildes_runner(texto: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn")


def _llamadas_del_turno(mensajes: list, inicio: int) -> list[tuple[str, dict]]:
    salida = []
    for m in mensajes[inicio + 1 :]:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for llamada in m.get("tool_calls") or []:
            funcion = (llamada.get("function") or {}) if isinstance(llamada, dict) else {}
            try:
                argumentos = json.loads(funcion.get("arguments") or "{}")
            except (TypeError, ValueError):
                argumentos = {}
            salida.append((funcion.get("name") or "", argumentos if isinstance(argumentos, dict) else {}))
    return salida


_HUELLA_DE_PLANTILLA = re.compile(
    r'<script type="application/json" id="hq-spec">.*?</script>'
    r'<script type="application/json" id="hq-huella">\{"sha256": "([0-9a-f]{64})"\}</script>',
    re.S,
)


_NO_INCLUYE = re.compile(r"\bno (incluye|incluy[óo]|tiene|trae|permite|es posible|puede)\b", re.I)

_FUNCIONES_PROMETIDAS = {
    "breadcrumb": (re.compile(r"breadcrumb|migas de pan", re.I), re.compile(r"breadcrumb|migas", re.I)),
    "botón Volver": (re.compile(r"bot[óo]n\s+[«\"“]?volver", re.I), re.compile(r"volver|\bback\b", re.I)),
    "simulador": (re.compile(r"simulador", re.I), re.compile(r"simul", re.I)),
    "matriz de permisos": (re.compile(r"matriz", re.I), re.compile(r"matriz|matrix", re.I)),
    "candado": (re.compile(r"candado", re.I), re.compile(r"candado|lock|🔒|128274|1f512", re.I)),
}


def funciones_prometidas_sin_html(sin_media: str, texto: str, mensajes, inicio: int, cwd: str | None) -> list[dict]:
    archivos = _htmls_del_turno(texto, mensajes, inicio, cwd)
    if not archivos:
        return []
    contenido = []
    for archivo in archivos:
        try:
            contenido.append(archivo.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    if not contenido:
        return []
    html = "\n".join(contenido)
    prometidas = []
    for nombre, (en_respuesta, en_html) in _FUNCIONES_PROMETIDAS.items():
        lineas = [
            linea
            for linea in sin_media.split("\n")
            if en_respuesta.search(linea) and not _RECONOCE_QUE_NO.search(linea) and not _NO_INCLUYE.search(linea)
        ]
        if lineas and not en_html.search(html):
            prometidas.append(nombre)
    if not prometidas:
        return []
    return [{"tipo": "funciones_inexistentes", "frase": ", ".join(prometidas), "funciones": ", ".join(prometidas)}]


def filas_inventadas_en_excel(mensajes: list, inicio: int, mensaje: str) -> list[str]:
    texto = _sin_tildes_runner(mensaje or "").lower()
    propio = _sin_tildes_runner(_texto_propio_del_usuario(mensaje)).lower()
    if (
        len(propio) > 600
        or len(_NUMERO_EN_TEXTO.findall(propio)) < 6
        or _REFERENCIA_A_UN_ARCHIVO.search(re.sub(r"\[formato pedido en este mensaje:[^\]]*\]", " ", texto))
        or "conversacion reciente" in texto
    ):
        return []
    inventadas: list[str] = []
    generados = [
        argumentos for nombre, argumentos in _llamadas_del_turno(mensajes, inicio) if nombre == "hq_xlsx_generate"
    ]
    for argumentos in generados[-1:]:
        for hoja in argumentos.get("sheets") or []:
            for fila in (hoja.get("rows") or []) if isinstance(hoja, dict) else []:
                if not isinstance(fila, list) or not fila or not isinstance(fila[0], str):
                    continue
                etiqueta = _sin_tildes_runner(fila[0]).strip().lower()
                tiene_cifras = any(isinstance(v, int | float) and not isinstance(v, bool) for v in fila[1:])
                if (
                    not etiqueta
                    or not tiene_cifras
                    or _FILA_RESUMEN.match(etiqueta)
                    or _ETIQUETA_DE_PERIODO.search(etiqueta)
                ):
                    continue
                if not _etiqueta_en_el_mensaje(etiqueta, texto) and fila[0] not in inventadas:
                    inventadas.append(fila[0])
    return inventadas


_ETIQUETA_DE_PERIODO = re.compile(
    r"^(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[a-z]*\.?(\s+(de\s+)?\d{2,4})?$|^(q|t|s)[1-4]\b|"
    r"trimestre|semestre|semana|^(19|20)\d{2}$|^\d{1,2}[-/]\d{1,2}([-/]\d{2,4})?$"
)
_PALABRAS_GENERICAS = frozenset({"sucursal", "zona", "region", "area", "sede", "planta", "local", "tienda", "equipo"})


def _etiqueta_en_el_mensaje(etiqueta: str, texto: str) -> bool:
    numeros = _NUMERO_EN_TEXTO.findall(etiqueta)
    if numeros:
        return bool(re.search(rf"(?<!\w){re.escape(etiqueta)}(?!\w)", texto))
    palabras = [p for p in re.findall(r"[a-zñ]{3,}", etiqueta) if p not in _PALABRAS_GENERICAS]
    if not palabras:
        return bool(numeros)
    return any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", texto) for p in palabras)


def codigos_mostrados(respuesta: str) -> list[str]:
    codigos = []
    for coincidencia in _CODIGO_MOSTRADO.finditer(respuesta or ""):
        codigo = (coincidencia.group(1) or coincidencia.group(2) or "").upper()
        if not codigo or codigo in _CODIGOS_DE_EJEMPLO or (codigo.isalpha() and len(codigo) != _LARGO_CODIGO):
            continue
        if codigo not in codigos:
            codigos.append(codigo)
    return codigos


def _codigo_respaldado(codigo: str, respaldo: str) -> bool:
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(codigo)}(?![A-Z0-9])", respaldo.upper()))


def _estado_resultado(contenido) -> str:
    texto = contenido if isinstance(contenido, str) else json.dumps(contenido, ensure_ascii=False, default=str)
    try:
        datos = json.loads(texto)
    except (TypeError, ValueError):
        datos = None
    if isinstance(datos, dict):
        if datos.get("requiere_aprobacion"):
            return "pendiente"
        if datos.get("success") is False or (datos.get("error") and not datos.get("success")):
            return "fallo"
        return "ok"
    return "fallo" if texto.lstrip().lower().startswith(("error", "traceback")) else "ok"


def resultados_de_herramientas(mensajes: list, inicio: int) -> list[tuple[str, str, str]]:
    nombres = {}
    salida = []
    for m in mensajes[inicio + 1 :]:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                if isinstance(llamada, dict):
                    nombres[llamada.get("id")] = (llamada.get("function") or {}).get("name") or ""
        elif m.get("role") == "tool":
            nombre = nombres.get(m.get("tool_call_id")) or m.get("name") or ""
            contenido = m.get("content")
            texto = contenido if isinstance(contenido, str) else json.dumps(contenido, ensure_ascii=False, default=str)
            salida.append((nombre, _estado_resultado(texto), texto or ""))
    return salida


def _textos_de_respaldo(mensajes: list) -> str:
    partes = []
    for m in mensajes:
        if isinstance(m, dict) and m.get("role") in ("tool", "user"):
            contenido = m.get("content")
            if isinstance(contenido, str) and contenido.startswith("[Nota del sistema]"):
                continue
            partes.append(contenido if isinstance(contenido, str) else json.dumps(contenido, default=str))
    return "\n".join(partes)


def _clave_enlace(url: str) -> tuple[str, bool]:
    from urllib.parse import unquote, urlsplit

    partes = urlsplit(url)
    ruta = unquote(partes.path)
    host = partes.netloc.lower()
    if "1drv.ms" in host or re.search(r"/:[a-z]:/", ruta):
        return unquote(url.split("?")[0]).lower(), True
    segmentos = [x for x in ruta.split("/") if x]
    return f"{host}/{'/'.join(segmentos[:2])}".lower(), False


def enlace_respaldado(url: str, respaldo: str) -> bool:
    from urllib.parse import unquote

    clave, exacta = _clave_enlace(url)
    texto = unquote(respaldo).lower()
    if exacta:
        return clave in texto or clave.rsplit("/", 1)[-1] in texto
    return clave in texto


def _archivo_existe(ruta: str, cwd: str | None) -> bool:
    ruta = ruta.strip().strip("`'\"")
    if not ruta or _RUTA_DE_PC.match(ruta):
        return False
    candidato = Path(ruta)
    if candidato.is_file():
        return True
    return bool(cwd) and (Path(cwd) / ruta).is_file()


_REVISOR_HTML = (
    os.environ.get("HERMESHQ_REVISOR_HTML", ""),
    str(Path(__file__).resolve().parents[1] / "assets" / "revisor-html" / "revisar.js"),
)
_EXTENSIONES_HTML = (".html", ".htm")
_revisiones_html: dict[tuple[str, float, int], dict | None] = {}


def revisar_html_js(ruta: Path) -> dict | None:
    script = next((c for c in _REVISOR_HTML if c and Path(c).is_file()), None)
    nodo = shutil.which("node")
    if not script or not nodo:
        return None
    try:
        ruta = ruta.resolve()
        estado = ruta.stat()
    except OSError:
        return None
    clave = (str(ruta), estado.st_mtime, estado.st_size)
    if clave in _revisiones_html:
        return _revisiones_html[clave]
    temporal = tempfile.gettempdir()
    try:
        proceso = subprocess.run(
            [nodo, "--max-old-space-size=512", script, str(ruta)],
            capture_output=True,
            text=True,
            timeout=40,
            cwd=temporal,
            env={"PATH": os.environ.get("PATH", ""), "HOME": temporal},
        )
        resultado = json.loads(proceso.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        resultado = None
    if not isinstance(resultado, dict) or not isinstance(resultado.get("errores_carga"), list):
        resultado = None
    _revisiones_html[clave] = resultado
    return resultado


def detalle_error_js(error: dict) -> str:
    lugar = ", ".join(
        parte
        for parte in (
            f"al hacer clic en {error['elemento']}" if error.get("elemento") else "",
            f"línea {error['linea']}" if error.get("linea") else "",
        )
        if parte
    )
    texto = f"{lugar}: {error.get('mensaje', '')}" if lugar else str(error.get("mensaje", ""))
    return f"{texto} — «{error['codigo']}»" if error.get("codigo") else texto


_NOMBRE_DE_ARCHIVO = re.compile(
    r"[^\s/\\`'\"«»()\[\]:]+\.(?:html?|xlsx|xlsm|xls|csv|docx?|pptx?|pdf|png|jpe?g|txt|json|zip|vsdx?|pbix)\b", re.I
)


def _archivo_visto_al_leer(respuesta: str, resultados) -> bool:
    nombres = {n.lower() for n in _NOMBRE_DE_ARCHIVO.findall(_MARCA_MEDIA.sub(" ", _LINEA_MEDIA.sub("", respuesta)))}
    if not nombres:
        return True
    leido = " ".join(c for nombre, estado, c in resultados if estado == "ok" and nombre in _LECTURAS_REMOTAS).lower()
    return any(n in leido for n in nombres)


_AFIRMA_CORREGIDO = re.compile(
    r"\b(ya )?(lo|la|los|las) correg[íi]\b|\bcorreg[íi] (el|la|los|las|ese|esa|este|esta)\b|"
    r"\bqued[óo] (corregid|arreglad|bien\b|v[áa]lid)|"
    r"\b(ya )?est[áa] (bien|correct[oa]|corregid[oa]|arreglad[oa]|v[áa]lid[oa])\b|"
    r"\b(c[óo]digo|archivo|html|javascript|p[áa]gina)\b[^.\n]{0,30}\b(es|est[áa]|qued[óo]) v[áa]lid[oa]\b|"
    r"\berror\w*\b[^.\n]{0,60}\bya no existe\b|\bya no (tiene|hay|existe)\w* (el |ning[úu]n )?error|"
    r"\b(funciona|abre) (bien|correctamente)\b|\bsin errores\b",
    re.I,
)


def _htmls_del_turno(texto: str, mensajes, inicio: int, cwd: str | None) -> list[Path]:
    base = Path(cwd or ".")
    rutas: list[str] = []
    for m in (mensajes or [])[inicio:]:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                argumentos = str(((llamada or {}).get("function") or {}).get("arguments") or "")
                rutas.extend(_NOMBRE_DE_RUTA_HTML.findall(argumentos))
    rutas.extend(_NOMBRE_DE_RUTA_HTML.findall(texto or ""))
    vistos: list[Path] = []
    for ruta in rutas:
        candidato = Path(ruta) if Path(ruta).is_absolute() else base / ruta
        try:
            if candidato.is_file() and candidato.resolve() not in vistos:
                vistos.append(candidato.resolve())
        except OSError:
            continue
    return vistos[:6]


_NOMBRE_DE_RUTA_HTML = re.compile(r"[\w./-]*[\w-]\.html?\b", re.I)


def html_dado_por_corregido(sin_media: str, texto: str, mensajes, inicio: int, cwd: str | None) -> list[dict]:
    frases = [o for o in _frases_afirmadas(sin_media) if _afirma(o, _AFIRMA_CORREGIDO)]
    if not frases:
        return []
    for archivo in _htmls_del_turno(texto, mensajes, inicio, cwd):
        revision = revisar_html_js(archivo)
        if revision and revision["errores_carga"]:
            error = revision["errores_carga"][0]
            return [
                {
                    "tipo": "html_no_corregido",
                    "frase": frase,
                    "archivo": archivo.name,
                    "detalle": detalle_error_js(error),
                    "resumen": _resumen_error_js(error),
                }
                for frase in frases
            ]
    return []


def _resumen_error_js(error: dict) -> str:
    mensaje = str(error.get("mensaje") or "error de JavaScript")
    return f"línea {error['linea']}: {mensaje}" if error.get("linea") else mensaje


def errores_js_de_adjuntos(texto: str, cwd: str | None) -> list[dict]:
    pendientes: list[dict] = []
    vistas: set[str] = set()
    for coincidencia in _LINEA_MEDIA.finditer(texto or ""):
        frase = coincidencia.group(1)[:300]
        ruta = coincidencia.group(1).strip().strip("`'\"")
        if frase in vistas or not ruta.lower().endswith(_EXTENSIONES_HTML) or not _archivo_existe(ruta, cwd):
            continue
        vistas.add(frase)
        archivo = Path(ruta) if Path(ruta).is_file() else Path(cwd or "") / ruta
        revision = revisar_html_js(archivo)
        if not revision:
            continue
        if revision["errores_carga"]:
            pendientes.append(
                {
                    "tipo": "html_roto",
                    "frase": frase,
                    "detalle": "; ".join(detalle_error_js(e) for e in revision["errores_carga"][:2]),
                    "resumen": _resumen_error_js(revision["errores_carga"][0]),
                }
            )
        elif revision.get("errores_clic"):
            primero = revision["errores_clic"][0]
            pendientes.append(
                {
                    "tipo": "html_clic",
                    "frase": frase,
                    "detalle": "; ".join(detalle_error_js(e) for e in revision["errores_clic"][:2]),
                    "elemento": primero.get("elemento") or "un elemento",
                    "resumen": _resumen_error_js(primero),
                }
            )
        elif int(revision.get("con_onclick") or 0) >= 6 and int(revision.get("sin_efecto") or 0) >= 0.8 * int(
            revision["con_onclick"]
        ):
            sin, total = int(revision["sin_efecto"]), int(revision["con_onclick"])
            pendientes.append(
                {
                    "tipo": "html_sin_efecto",
                    "frase": frase,
                    "detalle": "",
                    "elemento": revision.get("ejemplo_sin_efecto") or "un elemento",
                    "resumen": f"al hacer clic en {sin} de {total} elementos con onclick la página no cambia "
                    f"(por ejemplo {revision.get('ejemplo_sin_efecto') or 'un elemento'})",
                }
            )
    return pendientes


_CARPETAS_DE_SALIDA = ("output", "outputs", "uploads")


_MARGEN_MAS_RECIENTE_S = 2


def _rastro_del_turno(mensajes, inicio: int) -> str:
    partes: list[str] = []
    for m in (mensajes or [])[inicio:]:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            partes.append(str(m.get("content") or ""))
        elif m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                argumentos = str(((llamada or {}).get("function") or {}).get("arguments") or "")
                try:
                    argumentos = json.dumps(json.loads(argumentos), ensure_ascii=False)
                except ValueError:
                    pass
                partes.append(argumentos)
    return " ".join(partes)


def rutas_media_corregidas(texto: str, cwd: str | None, mensajes, inicio: int = 0) -> str:
    if not texto or not cwd:
        return texto
    rastro = _rastro_del_turno(mensajes, inicio)
    resultados = " ".join(
        str(m.get("content") or "")
        for m in (mensajes or [])[inicio:]
        if isinstance(m, dict) and m.get("role") == "tool"
    )
    base = Path(cwd)
    raiz = base.resolve()

    def dentro(candidato: Path) -> bool:
        try:
            return candidato.is_file() and raiz in candidato.resolve().parents
        except OSError:
            return False

    def corregir(coincidencia):
        ruta = coincidencia.group(1)
        limpia = ruta.strip().strip("`'\"")
        if re.match(r"https?://", limpia) or _archivo_existe(limpia, cwd):
            return coincidencia.group(0)
        partes = [p for p in re.split(r"[\\/]+", limpia) if p]
        del_pc = bool(_RUTA_DE_PC.match(limpia))
        if not partes or ".." in partes or partes[-1] not in (resultados if del_pc else rastro):
            return coincidencia.group(0)
        nombre = partes[-1]
        if not del_pc:
            for desde in range(len(partes) - 1):
                sufijo = Path(*partes[desde:])
                if dentro(base / sufijo):
                    return coincidencia.group(0).replace(ruta, sufijo.as_posix())
        carpetas = _CARPETAS_DE_SALIDA if del_pc else (*_CARPETAS_DE_SALIDA, "work", "")
        candidatos = [base / carpeta / nombre for carpeta in carpetas if dentro(base / carpeta / nombre)]
        if len(candidatos) > 1 and not del_pc:
            candidatos.sort(key=lambda c: c.stat().st_mtime, reverse=True)
            if candidatos[0].stat().st_mtime - candidatos[1].stat().st_mtime > _MARGEN_MAS_RECIENTE_S:
                candidatos = candidatos[:1]
        if len(candidatos) != 1:
            return coincidencia.group(0)
        return coincidencia.group(0).replace(ruta, candidatos[0].relative_to(base).as_posix())

    return _LINEA_MEDIA.sub(corregir, texto)


_PIDE_EL_ARCHIVO_AQUI = re.compile(
    r"\b(adjunt\w*|env[ií]a(?:me|lo|la)\w*|m[aá]nda(?:me|lo|la)\w*|p[aá]sa(?:me|lo|la)\w*|descarg\w*)\b|"
    r"\b(?:aqu[ií]|por (?:este|el) chat|en el chat)\b",
    re.I,
)


def adjunto_quitado_tras_subida(texto: str, mensajes, inicio: int, mensaje_usuario: str) -> str:
    if not texto or "MEDIA:" not in texto or _PIDE_EL_ARCHIVO_AQUI.search(mensaje_usuario or ""):
        return texto
    subidos: list[str] = []
    nombres = {}
    for m in (mensajes or [])[inicio:]:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for llamada in m.get("tool_calls") or []:
                funcion = (llamada or {}).get("function") or {}
                if funcion.get("name") == "sharepoint_upload_file":
                    try:
                        ruta = json.loads(funcion.get("arguments") or "{}").get("file_path")
                    except ValueError:
                        ruta = None
                    nombres[(llamada or {}).get("id")] = str(ruta or "")
    for m in (mensajes or [])[inicio:]:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") in nombres:
            try:
                salida = json.loads(m.get("content") or "{}")
            except ValueError:
                continue
            if isinstance(salida, dict) and salida.get("success") is True and nombres[m["tool_call_id"]]:
                subidos.append(re.split(r"[\\/]", nombres[m["tool_call_id"]])[-1].lower())
    if not subidos:
        return texto

    def quitar(coincidencia):
        ruta = re.split(r"[\\/]", coincidencia.group(1).strip().strip("`'\""))[-1].lower()
        return "" if ruta in subidos else coincidencia.group(0)

    limpio = _LINEA_MEDIA.sub(quitar, texto)
    return re.sub(r"\n{3,}", "\n\n", limpio).rstrip() if limpio != texto else texto


def _frases_afirmadas(respuesta: str):
    texto = _CITADO.sub(" ", _LINEA_CITA_O_TABLA.sub("", respuesta or ""))
    for oracion in _ORACIONES.split(texto):
        if not oracion or not oracion.strip() or _es_pregunta(oracion) or _REFIERE_AL_PASADO.search(oracion):
            continue
        if _PIDE_O_PROPONE.search(oracion):
            continue
        yield oracion.strip()


def _tipo_marcado_o_preferencia(afirmacion: str, oracion: str) -> str | None:
    if _LEIDO.search(oracion) and _afirma(afirmacion, _AFIRMA_MARCADO):
        return "marcado"
    if _OBJETO_PREFERENCIA.search(oracion) and _afirma(afirmacion, _AFIRMA_GUARDADO):
        return "preferencia"
    return None


def _antes_de_para_que(respuesta: str):
    texto = _CITADO.sub(" ", _LINEA_CITA_O_TABLA.sub("", respuesta or ""))
    for oracion in _ORACIONES.split(texto):
        if (
            not oracion
            or _es_pregunta(oracion)
            or _REFIERE_AL_PASADO.search(oracion)
            or _HABLA_DE_SKILL.search(oracion)
        ):
            continue
        propuesta = _PIDE_O_PROPONE.search(oracion)
        if not propuesta or not propuesta.group(0).lower().startswith("para que"):
            continue
        previa = oracion[: propuesta.start()]
        if previa.strip() and not _PIDE_O_PROPONE.search(previa):
            yield previa.strip(), oracion


def _afirma(oracion: str, patron) -> bool:
    return any(not _NEGACION.search(oracion[: c.start()]) for c in patron.finditer(oracion))


_BORRADOR_DE_CORREO = re.compile(
    r"\bborradores?\b.{0,80}\b(outlook|correo|mail)\b|\b(outlook|correo|mail)\b.{0,80}\bborradores?\b", re.IGNORECASE
)


def _tipo_de_frase(oracion: str, pidio_guardar: bool = False) -> str | None:
    if _HABLA_DE_SKILL.search(oracion) or _BORRADOR_DE_CORREO.search(oracion):
        return None
    remoto = bool(_LUGAR_REMOTO.search(oracion)) and not _LUGAR_LOCAL.search(oracion)
    if (pidio_guardar and not _LUGAR_LOCAL.search(oracion) and _afirma(oracion, _VERBO_SUBIDA)) or (
        remoto and (_afirma(oracion, _VERBO_SUBIDA) or _afirma(oracion, _VERBO_SUBIDA_CON_LUGAR))
    ):
        return "subida"
    tipo = _tipo_marcado_o_preferencia(oracion, oracion)
    if tipo:
        return tipo
    confirma = bool(_CONFIRMA.search(oracion))
    if _OBJETO_CALENDARIO.search(oracion) and (
        _afirma(oracion, _VERBO_CALENDARIO)
        or (confirma and (_afirma(oracion, _NOMINAL_CALENDARIO) or _afirma(oracion, _IMPERSONAL_CALENDARIO)))
    ):
        return "calendario"
    if _afirma(oracion, _VERBO_PROGRAMACION) or (_CONFIRMA.search(oracion) and _afirma(oracion, _NOMINAL_PROGRAMACION)):
        return "programacion"
    if _afirma(oracion, _ENVIO_A_TERCERO) and not re.search(r"\bte (envi|reenvi|mand)", oracion, re.I):
        return "correo"
    if _OBJETO_CORREO.search(oracion) and (
        _afirma(oracion, _VERBO_CORREO)
        or (confirma and (_afirma(oracion, _NOMINAL_CORREO) or _afirma(oracion, _IMPERSONAL_CORREO)))
    ):
        return "correo"
    return None


_RUTA_ONEDRIVE_PEDIDA = re.compile(r"[A-Za-z]:\\Users\\[^\\]+\\OneDrive[^\\]*\\(.+)")
_RUTA_CON_UNIDAD = re.compile(r"[A-Za-z]:\\")
_RUTA_SIN_UNIDAD = re.compile(r"[^\\\n]+(?:\\[^\\\n]+)+\\?")
_NOMBRA_CARPETA = re.compile(r"\b(ruta|carpeta|directorio|biblioteca)\b", re.I)
_SEPARA_RUTAS = re.compile(r"\s+\d+\s*[.)-]+\s*(?=[A-Za-z]:\\)|\n+|\s+y\s+(?=[A-Za-z]:\\)")
_SIGUE_TEXTO = re.compile(
    r"\s*[,;:(\"'«»“”]|\.\s|\s+(y|e|con|para|que|donde|como|pero|porque|el|la|los|las|por favor)\b"
)
_ES_ARCHIVO = re.compile(
    r"\.(html?|xlsx|xlsm|xls|csv|docx?|pptx?|pdf|png|jpe?g|gif|txt|json|md|zip|vsdx?|pbix)\b", re.I
)


_PIDE_RENOMBRAR = re.compile(r"\brenombr|\bcambi\w* (el |su )?nombre\b|\bnombre original\b", re.I)
_AFIRMA_RENOMBRE = re.compile(
    r"\b(renombr[ée]|renombrad[oa]s?|ahora se llama|cambi[ée] (el|su) nombre|qued[óo] con el nombre)\b", re.I
)
_RENOMBRE_INTERNO = re.compile(r"\b(hoja|pesta[ñn]a|columna|fila|secci[óo]n|t[íi]tulo|tabla|diapositiva)s?\b", re.I)
_RECONOCE_COPIA = re.compile(
    r"\bcopia\b|\bno (puedo|pude|tengo c[óo]mo) renombrar|\bsigue (existiendo|estando|ah[íi]|en la carpeta)|"
    r"\b(ambos|los dos) archivos\b",
    re.I,
)


def renombre_sin_herramienta(respuesta: str, mensaje: str) -> str | None:
    if not _PIDE_RENOMBRAR.search(_texto_propio_del_usuario(mensaje) or ""):
        return None
    if _RECONOCE_COPIA.search(respuesta or ""):
        return None
    for oracion in _frases_afirmadas(respuesta):
        if _afirma(oracion, _AFIRMA_RENOMBRE) and not _RENOMBRE_INTERNO.search(oracion):
            return oracion
    return None


_RECONOCE_QUE_NO = re.compile(
    r"\bno (pude|se pudo|logr[ée]|puedo|fue posible|qued[óo]|alcanc[ée])\b|\bfall[óo]\b", re.I
)


def _plano_carpeta(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", sin_acentos.lower()).strip(" .\\/\"'`«»“”")


def _segmentos(ruta: str) -> list[str]:
    return [s for s in (_plano_carpeta(p) for p in re.split(r"[\\/]+", ruta or "")) if s]


def _rutas_pedidas(mensaje: str) -> list[tuple[list[str], bool, str]]:
    rutas = []
    for trozo in _SEPARA_RUTAS.split(mensaje or ""):
        coincidencia = _RUTA_ONEDRIVE_PEDIDA.search(trozo)
        if coincidencia:
            ruta, exacta = coincidencia.group(1), True
        elif _RUTA_CON_UNIDAD.search(trozo) or not _NOMBRA_CARPETA.search(trozo):
            continue
        else:
            suelta = _RUTA_SIN_UNIDAD.search(trozo)
            if not suelta:
                continue
            ruta, exacta = suelta.group(0), False
        partes = [p.strip() for p in ruta.split("\\") if _plano_carpeta(p)]
        if partes:
            corte = _SIGUE_TEXTO.search(partes[-1])
            if _ES_ARCHIVO.search(partes[-1][: corte.start()] if corte else partes[-1]):
                partes = partes[:-1]
        segmentos = [_plano_carpeta(p) for p in partes]
        if len(segmentos) < (1 if exacta else 2):
            continue
        ultimo = partes[-1]
        corte = _SIGUE_TEXTO.search(ultimo)
        rutas.append((segmentos, exacta, (ultimo[: corte.start()] if corte else ultimo).strip()))
    return rutas


def _ruta_cumplida(pedida: list[str], exacta: bool, destino: list[str]) -> bool:
    if len(destino) < len(pedida):
        return False
    for i, (p, d) in enumerate(zip(pedida, destino)):
        if p == d:
            continue
        ultimo = i == len(pedida) - 1
        if i == 0 and not exacta and (p.endswith(" " + d) or (ultimo and f" {d} " in f" {p} ")):
            continue
        if ultimo and p.startswith(d) and _SIGUE_TEXTO.match(p[len(d) :]):
            continue
        return False
    return True


def carpetas_pedidas_sin_subida(mensaje: str, resultados: list) -> list[str]:
    pedidas = _rutas_pedidas(mensaje)
    if not pedidas:
        return []
    destinos = []
    for nombre, estado, contenido in resultados:
        if nombre != "sharepoint_upload_file" or estado != "ok":
            continue
        try:
            datos = json.loads(contenido or "{}")
        except (TypeError, ValueError):
            return []
        carpeta = str((datos or {}).get("folder") or "").strip().strip("/")
        if not carpeta:
            return []
        destinos.append(_segmentos(carpeta))
    if not destinos:
        return []
    faltan = []
    for segmentos, exacta, corto in pedidas:
        if not any(_ruta_cumplida(segmentos, exacta, d) for d in destinos):
            faltan.append(corto)
    return list(dict.fromkeys(faltan))


_SOLO_MUEVEN_ARCHIVOS = ("sharepoint_", "ms365_", "teams_", "hq_schedule_")


_PIDE_ENTREGA_DE_ARCHIVO = re.compile(
    r"\b(?:enviame\w*|mandame\w*|pasame\w*|adjunta\w*|entregame\w*|dame|comparteme\w*)\b[^.?!\n]{0,60}"
    r"\b(?:zip|archivo|excel|xlsx|pdf|word|docx|pptx|powerpoint|presentacion|html|csv|documento|planilla|descargable)\b"
    r"|\bno (?:tengo|veo|me llego|recibi|aparece|encuentro)\b[^.?!\n]{0,30}"
    r"\b(?:descargable|archivo|adjunto|zip)\b"
)
_ENTREGA_POR_OTRO_CANAL = re.compile(r"\b(?:correo|mail|teams|onedrive|sharepoint|whatsapp|carpeta|enlace|link)\b")


def entrega_de_archivo_sin_adjunto(propio: str, respuesta: str, resultados) -> bool:
    plano = _sin_tildes_runner(propio or "").lower()
    texto = respuesta or ""
    if resultados or len(plano) > 300 or _LINEA_MEDIA.search(texto):
        return False
    if not _PIDE_ENTREGA_DE_ARCHIVO.search(plano) or _ENTREGA_POR_OTRO_CANAL.search(plano):
        return False
    return not (_RECONOCE_QUE_NO.search(texto) or "?" in texto or "segundo plano" in texto.lower())


def verificar_afirmaciones(
    respuesta: str,
    mensajes: list,
    inicio: int,
    cwd: str | None = None,
    programada: bool = False,
    mensaje: str = "",
    aprobado: bool = False,
) -> list[dict]:
    texto = respuesta or ""
    if not texto.strip():
        return []
    resultados = resultados_de_herramientas(mensajes, inicio)
    estados = {}
    for nombre, estado, contenido in resultados:
        for tipo, herramientas in _EFECTOS_POR_TIPO.items():
            if nombre in herramientas:
                estados.setdefault(tipo, set()).add(estado)
        if estado == "ok" and nombre not in _HERRAMIENTAS_SOLO_LECTURA and _URL_REMOTA.search(contenido):
            estados.setdefault("subida", set()).add("ok")
    pidio_guardar = bool(_LUGAR_REMOTO.search(mensaje or "") and _PIDE_GUARDAR.search(mensaje or ""))
    leyo_remoto = any(estado == "ok" and nombre in _LECTURAS_REMOTAS for nombre, estado, _ in resultados)
    leyo_correo = any(estado == "ok" and nombre in _LECTURAS_CORREO for nombre, estado, _ in resultados)
    hubo_efecto = any(
        estado == "ok" and nombre not in _HERRAMIENTAS_SOLO_LECTURA and not nombre.startswith(_SOLO_MUEVEN_ARCHIVOS)
        for nombre, estado, _ in resultados
    )
    pide_estado = len(mensaje or "") < 300 and _PIDE_ESTADO.search(mensaje or "")
    pedido_archivo = pedido_con_efecto(mensaje or "") in ("edicion", "creacion")
    frases_sueltas = not (_PROPONE_ANTES.search(texto) or pide_estado)
    pendientes = []
    vistos = set()
    sin_media = _MARCA_MEDIA.sub(" ", _LINEA_MEDIA.sub("", texto))
    for oracion in _frases_afirmadas(sin_media) if frases_sueltas else ():
        tipo = _tipo_de_frase(oracion, pidio_guardar)
        if (
            tipo == "subida"
            and leyo_remoto
            and _EXISTENCIA.search(oracion)
            and _archivo_visto_al_leer(texto, resultados)
        ):
            continue
        if tipo == "correo" and (
            (leyo_correo and _VERIFICA_ENVIO.search(oracion))
            or (_OBJETO_DE_EVENTO.search(oracion) and "ok" in estados.get("calendario", set()))
        ):
            continue
        if tipo is None:
            primera_persona = (
                re.search(rf"\b({_PRODUJO})\b", oracion, re.I) or _MARCA_FUERTE.search(oracion) or pedido_archivo
            )
            if primera_persona and afirma_archivo(oracion) and afirma_hecho(oracion) and not hubo_efecto:
                tipo = "archivo"
            else:
                continue
        elif (programada and tipo == "programacion") or (aprobado and tipo != "subida"):
            continue
        if tipo != "archivo":
            obtenidos = estados.get(tipo, set())
            if "ok" in obtenidos or (tipo in _PENDIENTE_CUENTA and "pendiente" in obtenidos):
                continue
            if "pendiente" in obtenidos:
                tipo = f"{tipo}_pendiente"
        if (tipo, oracion) in vistos:
            continue
        vistos.add((tipo, oracion))
        pendientes.append({"tipo": tipo, "frase": oracion[:300]})
    for previa, oracion in _antes_de_para_que(sin_media) if frases_sueltas and not aprobado else ():
        tipo = _tipo_marcado_o_preferencia(previa, oracion)
        if not tipo or "ok" in estados.get(tipo, set()) or any(p["tipo"] == tipo for p in pendientes):
            continue
        pendientes.append({"tipo": tipo, "frase": previa[:300]})
    respaldo = None
    for url in dict.fromkeys(_URL_REMOTA.findall(texto)):
        url = url.rstrip(".,;:*_")
        if respaldo is None:
            respaldo = _textos_de_respaldo(mensajes)
        if not enlace_respaldado(url, respaldo):
            pendientes.append({"tipo": "enlace", "frase": url[:300]})
    propio = _texto_propio_del_usuario(mensaje)
    if (
        len(propio) <= 300
        and _PREGUNTA_UBICACION.search(propio)
        and _OBJETO_ARCHIVO.search(propio)
        and not any(estado == "ok" and nombre in _VERIFICAN_UBICACION for nombre, estado, _ in resultados)
    ):
        pendientes.append({"tipo": "ubicacion", "frase": (mensaje or "")[:160]})
    faltantes = [
        f
        for f in carpetas_pedidas_sin_subida(propio, resultados)
        if not any(
            _plano_carpeta(f) in _plano_carpeta(o) and _RECONOCE_QUE_NO.search(o) for o in _ORACIONES.split(sin_media)
        )
    ]
    if faltantes and any(_tipo_de_frase(o, pidio_guardar) == "subida" for o in _frases_afirmadas(sin_media)):
        pendientes.append({"tipo": "subida_otra_carpeta", "frase": ", ".join(faltantes)[:300]})
    renombre = renombre_sin_herramienta(sin_media, mensaje)
    if renombre:
        pendientes.append({"tipo": "renombre", "frase": renombre[:300]})
    for coincidencia in _LINEA_MEDIA.finditer(texto):
        ruta = coincidencia.group(1)
        if re.match(r"https?://", ruta.strip("`'\"")):
            continue
        if not _archivo_existe(ruta, cwd):
            tipo = "adjunto_pc" if _RUTA_DE_PC.match(ruta.strip("`'\"")) else "adjunto"
            pendientes.append({"tipo": tipo, "frase": ruta[:300], "sin_herramientas": not resultados})
    if entrega_de_archivo_sin_adjunto(propio, texto, resultados):
        pendientes.append({"tipo": "entrega_sin_archivo", "frase": propio.strip()[:160], "sin_herramientas": True})
    pendientes.extend(errores_js_de_adjuntos(texto, cwd))
    pendientes.extend(html_dado_por_corregido(sin_media, texto, mensajes, inicio, cwd))
    pendientes.extend(funciones_prometidas_sin_html(sin_media, texto, mensajes, inicio, cwd))
    inventadas = filas_inventadas_en_excel(mensajes, inicio, mensaje)
    if inventadas:
        pendientes.append({"tipo": "excel_inventado", "frase": ", ".join(inventadas[:6])})
    if (
        not programada
        and _TABLA_INFORME_CORREO.search(texto)
        and not any(nombre == "ms365_correo_sync" for nombre, _, _ in resultados)
    ):
        pendientes.append({"tipo": "informe_copiado", "frase": ""})
    codigos = codigos_mostrados(sin_media)
    if codigos:
        respaldo_turno = _textos_de_respaldo(mensajes[max(inicio, 0) :])
        consulto = any(nombre in _HERRAMIENTAS_DE_CODIGO for nombre, _, _ in resultados)
        for codigo in codigos:
            if not _codigo_respaldado(codigo, respaldo_turno):
                pendientes.append({"tipo": "codigo", "frase": codigo, "sin_consulta": not consulto})
    return pendientes


_PENDIENTES_DE_SUBIDA = frozenset({"subida", "subida_pendiente", "enlace"})
_PENDIENTES_DE_JS = frozenset({"html_roto", "html_clic", "html_no_corregido", "html_sin_efecto"})


def nota_verificacion(pendientes: list[dict]) -> str:
    lineas = [
        f"- «{p['frase']}»: {_MOTIVO_VERIFICACION[p['tipo']]}"
        + (f" (error: {p['detalle']})" if p.get("detalle") else "")
        + "."
        for p in pendientes
    ]
    if all(p["tipo"] in _PENDIENTES_DE_JS for p in pendientes):
        return (
            "[Nota del sistema] Abrí en un navegador de prueba el HTML que vas a entregar y su JavaScript tiene "
            "errores:\n" + "\n".join(lineas) + "\nCorrige ese mismo archivo: lee las líneas indicadas con read_file "
            "(offset cerca de la línea) y arréglalas con patch. Un SyntaxError suele estar en esa línea o justo antes "
            "(una comilla invertida ` o un paréntesis sin cerrar); un TypeError o ReferenceError, en un elemento o "
            "variable que no existe. Después vuelve a entregarlo con su línea MEDIA: y responde con el resultado real. "
            "Si no lo puedes corregir, dilo claramente; no digas que funciona." + _SIN_MENCIONAR_REVISION
        )
    ejemplo = (
        "por ejemplo, sharepoint_upload_file para guardar en la carpeta que pidió el usuario"
        if any(p["tipo"] in _PENDIENTES_DE_SUBIDA for p in pendientes)
        else "por ejemplo, crear el .zip con execute_code y el módulo zipfile dentro de outputs/ y adjuntarlo con su "
        "línea MEDIA:"
        if any(p["tipo"] in ("adjunto", "entrega_sin_archivo") and ".zip" in p["frase"].lower() for p in pendientes)
        else "por ejemplo, generar el archivo con su herramienta antes de adjuntarlo"
    )
    return (
        "[Nota del sistema] Revisé tu respuesta contra lo que realmente ejecutaron las herramientas en esta tarea y "
        "hay afirmaciones sin respaldo:\n" + "\n".join(lineas) + f"\nHaz ahora lo que falta con la herramienta "
        f"correspondiente ({ejemplo}) y responde con el resultado real. Si no puedes, dilo claramente. "
        "No afirmes nada que no hayas hecho." + _SIN_MENCIONAR_REVISION
    )


_HABLA_DEL_NOMBRE_NUEVO = re.compile(r"\b(nuevo nombre|renombr\w*|cambio de nombre|ahora se llama)\b", re.I)
_LISTO_SUELTO = re.compile(r"^(listo|hecho|perfecto)[.!]?\s*(?=\n|$)", re.I)
_AVISO_SIN_REPETIR = frozenset({"renombre"})


def _sin_oraciones(texto: str, patron) -> str:
    return "\n".join(
        " ".join(o for o in _ORACIONES.split(linea) if o and not patron.search(o)).strip()
        for linea in texto.split("\n")
    )


def respuesta_verificada(respuesta: str, pendientes: list[dict]) -> str:
    texto = respuesta or ""
    avisos = []
    sin_consulta = False
    sin_entregar = False
    for p in pendientes:
        if p["tipo"] == "ubicacion":
            avisos.append(_AVISO_VERIFICACION["ubicacion"])
            continue
        if p["tipo"] == "informe_copiado":
            sin_consulta = True
            avisos.append(_AVISO_VERIFICACION["informe_copiado"])
            continue
        if p["tipo"] == "excel_inventado":
            sin_consulta = True
            avisos.append(f"{_AVISO_VERIFICACION['excel_inventado']} Filas que no estaban: {p['frase']}.")
            continue
        if p["tipo"] == "codigo":
            patron = re.compile(rf"(?<![A-Za-z0-9]){re.escape(p['frase'])}(?![A-Za-z0-9])", re.IGNORECASE)
            texto = "\n".join(linea for linea in texto.split("\n") if not patron.search(linea))
            sin_consulta = sin_consulta or bool(p.get("sin_consulta"))
            avisos.append(_AVISO_VERIFICACION["codigo_sin_consulta" if p.get("sin_consulta") else "codigo"])
            continue
        if p["tipo"] == "funciones_inexistentes":
            for nombre in p["funciones"].split(", "):
                patron_respuesta = _FUNCIONES_PROMETIDAS[nombre][0]
                texto = "\n".join(
                    linea
                    for linea in texto.split("\n")
                    if not (
                        patron_respuesta.search(linea)
                        and not _RECONOCE_QUE_NO.search(linea)
                        and not _NO_INCLUYE.search(linea)
                    )
                )
            avisos.append(_AVISO_VERIFICACION["funciones_inexistentes"].format(funciones=p["funciones"]))
            continue
        if p["tipo"] == "html_no_corregido":
            texto = texto.replace(p["frase"], "")
            avisos.append(_AVISO_VERIFICACION["html_no_corregido"].format(nombre=p["archivo"], resumen=p["resumen"]))
            continue
        if p["tipo"] in _PENDIENTES_DE_JS:
            nombre = re.split(r"[\\/]", p["frase"].strip())[-1] or p["frase"]
            if p["tipo"] == "html_roto":
                texto = _LINEA_MEDIA.sub(lambda m, f=p["frase"]: "" if m.group(1)[:300] == f else m.group(0), texto)
                sin_entregar = True
            avisos.append(
                _AVISO_VERIFICACION[p["tipo"]].format(
                    nombre=nombre, elemento=p.get("elemento", ""), resumen=p.get("resumen", "error de JavaScript")
                )
            )
            continue
        if p["tipo"] in ("adjunto", "adjunto_pc"):
            texto = _LINEA_MEDIA.sub(lambda m, f=p["frase"]: "" if m.group(1)[:300] == f else m.group(0), texto)
            nombre = re.split(r"[\\/]", p["frase"].strip())[-1] or p["frase"]
            avisos.append(f"No pude adjuntar «{nombre}»: el archivo no existe.")
            continue
        texto = texto.replace(p["frase"], "")
        if p["tipo"] == "renombre":
            texto = _sin_oraciones(texto, _HABLA_DEL_NOMBRE_NUEVO)
        avisos.append(_AVISO_VERIFICACION[p["tipo"]])
    texto = _LISTO_SUELTO.sub("", re.sub(r"\n{3,}", "\n\n", texto).strip()).strip()
    aviso = "⚠️ Revisé lo que se ejecutó y corregí esta respuesta: " + " ".join(dict.fromkeys(avisos))
    if not all(p["tipo"] in _AVISO_SIN_REPETIR for p in pendientes):
        aviso += " Si lo necesitas, pídemelo de nuevo."
    solo_acciones = all(p["tipo"] in ("correo", "calendario") for p in pendientes)
    if solo_acciones and len(texto) < 600 and not _LINEA_MEDIA.search(texto):
        return aviso
    if (
        any(p["tipo"] == "archivo" for p in pendientes)
        and all(p["tipo"] in ("archivo", "adjunto") for p in pendientes)
        and len(_LINEA_MEDIA.sub("", texto)) < 600
    ):
        return aviso
    if all(p["tipo"] in ("archivo", "adjunto", "entrega_sin_archivo") for p in pendientes) and any(
        p.get("sin_herramientas") for p in pendientes
    ):
        return aviso
    if sin_entregar and not _LINEA_MEDIA.search(texto):
        return aviso
    if sin_consulta or (len(re.sub(r"[\W_]+", "", texto)) < 40):
        return aviso
    return f"{aviso}\n\n{texto}"


_AVISO_DESBOCADA = (
    "⚠️ Detuve esta respuesta: el modelo entró en un bucle ({motivo}) y no iba a terminar. "
    "Lo que ya se ejecutó con herramientas antes del corte quedó hecho; no te entrego un resumen a medias. "
    "Pídemelo de nuevo, idealmente más acotado."
)

_MOTIVOS_DESBOCADA = {
    "repeticion": "texto repetido",
    "largo": "respuesta demasiado larga",
    "tiempo": "generación demasiado lenta",
    "compactacion": "el contexto se compactó una y otra vez sin avanzar",
    "razonamiento": "razonamiento interno demasiado largo",
}


class GuardiaDesbocada:
    VENTANA = 300
    REPETICIONES = 5
    TRAMO = 30000

    def __init__(self, config: dict | None, ahora=time.monotonic):
        config = config or {}
        self.activa = bool(config.get("activa", True))
        self.max_caracteres = int(config.get("max_caracteres") or 120000)
        self.max_segundos = float(config.get("max_segundos") or 900)
        self.max_compactaciones = int(config.get("max_compactaciones") or 25)
        self._ahora = ahora
        self._agente = None
        self._texto: list[str] = []
        self._largo = 0
        self._razonamiento = 0
        self._ultimo_chequeo = 0
        self._inicio_paso: float | None = None
        self._paso = 0
        self._lock = threading.Lock()
        self.disparo: dict | None = None

    def vincular(self, agente) -> None:
        self._agente = agente

    def paso(self, n: int) -> None:
        with self._lock:
            self._paso = n
            self._texto = []
            self._largo = 0
            self._razonamiento = 0
            self._ultimo_chequeo = 0
            self._inicio_paso = self._ahora()
        self.revisar_compactaciones()

    def revisar_compactaciones(self) -> None:
        if not self.activa or self.disparo or self._agente is None:
            return
        compresor = getattr(self._agente, "context_compressor", None)
        veces = int(getattr(compresor, "compression_count", 0) or 0)
        if veces > self.max_compactaciones:
            self._disparar("compactacion")

    def herramientas(self) -> None:
        with self._lock:
            self._inicio_paso = None

    def texto(self, delta: str) -> None:
        if not self.activa or self.disparo:
            return
        with self._lock:
            self._texto.append(delta)
            self._largo += len(delta)
            if self._largo > self.max_caracteres:
                motivo = "largo"
            elif self._largo - self._ultimo_chequeo >= 2000 and self._largo >= 6000:
                self._ultimo_chequeo = self._largo
                motivo = "repeticion" if self._se_repite() else None
            else:
                motivo = None
        if motivo:
            self._disparar(motivo)

    def razonamiento(self, delta: str) -> None:
        if not self.activa or self.disparo:
            return
        with self._lock:
            self._razonamiento += len(delta or "")
            excede = self._razonamiento > self.max_caracteres
        if excede:
            self._disparar("razonamiento")

    def revisar_tiempo(self) -> None:
        if not self.activa or self.disparo:
            return
        with self._lock:
            inicio = self._inicio_paso
        if inicio is not None and self._ahora() - inicio > self.max_segundos:
            self._disparar("tiempo")

    def _se_repite(self) -> bool:
        texto = "".join(self._texto)
        self._texto = [texto]
        cola = texto[-self.VENTANA :]
        return len(cola) == self.VENTANA and texto[-self.TRAMO :].count(cola) >= self.REPETICIONES

    def _disparar(self, motivo: str) -> None:
        with self._lock:
            if self.disparo:
                return
            self.disparo = {
                "motivo": motivo,
                "paso": self._paso,
                "caracteres": self._largo,
                "segundos": None if self._inicio_paso is None else round(self._ahora() - self._inicio_paso, 1),
            }
        _diag(f"generación desbocada ({motivo}) en el paso {self._paso}: se interrumpe al agente")
        if self._agente is not None:
            try:
                self._agente.interrupt()
            except Exception as exc:  # noqa: BLE001
                _diag(f"no se pudo interrumpir al agente: {exc}")

    def vigilar(self, cada: float = 5.0) -> threading.Event:
        fin = threading.Event()

        def bucle() -> None:
            while not fin.wait(cada):
                self.revisar_tiempo()

        threading.Thread(target=bucle, name="guardia-desbocada", daemon=True).start()
        return fin

    def aviso(self) -> str:
        motivo = (self.disparo or {}).get("motivo", "")
        return _AVISO_DESBOCADA.format(motivo=_MOTIVOS_DESBOCADA.get(motivo, "salida sin control"))


def mensajes_sin_desborde(messages: list, aviso: str) -> list:
    limpios = list(messages)
    while (
        limpios
        and isinstance(limpios[-1], dict)
        and limpios[-1].get("role") == "assistant"
        and not limpios[-1].get("tool_calls")
    ):
        limpios.pop()
    return limpios + [{"role": "assistant", "content": aviso}]


def _guardia_accion_narrada(payload: dict) -> bool:
    return bool(payload.get("guardia_accion_narrada", True))


def _cerrar_turno(result: dict, emitted_chunks: list) -> tuple[str, list, list]:
    messages = result.get("messages", [])
    _diag(
        f"conversación terminada: partial={result.get('partial')} completed={result.get('completed')} "
        f"claves={sorted(result)} mensajes={len(result.get('messages') or [])}"
    )
    if result.get("partial") and not result.get("final_response"):
        raise RuntimeError(f"Hermes runtime stopped early: {result.get('error') or 'partial result'}")
    crudo = result.get("final_response")
    if not (isinstance(crudo, str) and crudo.strip()):
        crudo = "".join(chunk for chunk in emitted_chunks if isinstance(chunk, str))
    if es_respuesta_vacia_del_motor(crudo):
        nombres = [n for n in _HERRAMIENTAS_INICIADAS if n] or [
            n for n in _nombres_llamados_desde(messages, _indice_ultimo_usuario(messages)) if n
        ]
        if not nombres:
            raise RuntimeError(
                f"Hermes runtime stopped early: {MARCA_RESPUESTA_VACIA}: el modelo devolvió respuestas vacías en "
                "todos los reintentos; la solicitud no se procesó"
            )
        aviso = AVISO_SIN_RESPUESTA_FINAL.format(herramientas=", ".join(dict.fromkeys(nombres)))
        messages = mensajes_sin_desborde(messages, aviso)
        return aviso, messages, _extract_tool_calls(messages)
    herramientas_cortadas = corte_de_stream_sin_contenido(crudo)
    if herramientas_cortadas:
        raise RuntimeError(
            f"Hermes runtime stopped early: el stream se cortó al pedir {herramientas_cortadas}; "
            "la acción no se ejecutó"
        )
    final_response = texto_final(result, emitted_chunks)
    tool_calls = _extract_tool_calls(messages)
    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    if not final_response and not assistant_messages and not tool_calls:
        raise RuntimeError("Hermes runtime returned no assistant output")
    return final_response, messages, tool_calls


def _proteger_proceso() -> None:
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)
    except (OSError, AttributeError):
        _diag("no se pudo marcar el runner como no volcable")


def _ocultar_token_a_los_comandos() -> None:
    try:
        import tools.environments.local as entorno_local

        entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST = frozenset(
            entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST | set(SOLO_DEL_RUNNER)
        )
    except ImportError:
        _diag("no se pudo quitar el token del agente del entorno de los comandos")


def _precalentar() -> None:
    import shutil
    import tempfile

    hogar = tempfile.mkdtemp(prefix="hq-reserva-")
    previo = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = hogar
    try:
        from run_agent import AIAgent

        AIAgent(
            model="precalentamiento",
            provider="custom",
            base_url="http://127.0.0.1:9/v1",
            api_key="precalentamiento",
            quiet_mode=True,
            enabled_toolsets=[],
            platform="hermeshq",
            skip_memory=True,
            skip_context_files=True,
        )
    except Exception as exc:  # noqa: BLE001
        _diag(f"precalentamiento sin efecto: {exc}")
    finally:
        if previo is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previo
        shutil.rmtree(hogar, ignore_errors=True)


def main() -> int:
    _proteger_proceso()
    _abrir_salida()
    _desviar_salida_del_motor()
    if os.environ.pop("HERMESHQ_RUNNER_RESERVA", "") == "1":
        _precalentar()
    # Payload arrives via stdin (keeps the API key out of the process
    # environment, where it would be visible via /proc/<pid>/environ).
    # Fall back to the legacy env var for backwards compatibility.
    payload_raw = ""
    if not sys.stdin.isatty():
        try:
            payload_raw = sys.stdin.read().strip()
        except OSError:
            payload_raw = ""
    if not payload_raw:
        payload_raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not payload_raw:
        _emit({"event": "error", "error": "Missing task payload (stdin or HERMESHQ_TASK_PAYLOAD)"})
        return 1

    payload = json.loads(payload_raw)
    del payload_raw
    global _IDENTIDAD_EJECUCION
    identidad = payload.get("execution")
    if isinstance(identidad, dict) and identidad.get("attempt_id"):
        _IDENTIDAD_EJECUCION = {
            "execution_id": identidad.get("execution_id"),
            "attempt_id": identidad.get("attempt_id"),
            "lease_generation": identidad.get("lease_generation"),
        }
        _iniciar_latido(
            identidad.get("heartbeat_fd"), identidad.pop("heartbeat_key", None), identidad.get("heartbeat_interval")
        )
        _emit({"event": "attempt_started", "execution": dict(_IDENTIDAD_EJECUCION)})
        os.environ["HERMESHQ_EXECUTION_ID"] = str(identidad.get("execution_id") or "")
        os.environ["HERMESHQ_ATTEMPT_ID"] = str(identidad.get("attempt_id") or "")
        os.environ["HERMESHQ_LEASE_GENERATION"] = str(identidad.get("lease_generation") or "")
        credencial = identidad.pop("agent_token", None)
        if credencial:
            os.environ["HERMESHQ_AGENT_TOKEN"] = str(credencial)
    _ocultar_token_a_los_comandos()
    os.environ["HERMES_HOME"] = payload["hermes_home"]
    os.environ["HERMES_QUIET"] = "1"
    os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps(
        {
            "metadata": payload.get("metadata") or {},
            "task_id": payload.get("task_id"),
            "mensaje": _mensaje_para_herramientas(payload),
            "formato_pedido": _formato_pedido(payload),
            "respuesta_anterior": _ultima_respuesta(payload.get("conversation_history"))[:6000],
            "pedido_anterior": _ultimo_pedido_del_usuario(payload.get("conversation_history"))[:6000],
            "pedidos_previos": _pedidos_previos_del_usuario(payload.get("conversation_history")),
            "inicio": time.time(),
        }
    )
    os.environ.setdefault("TERM", "xterm-256color")

    os.chdir(payload["cwd"])
    envio_directo = (
        _envio_aprobado_directo(payload) or _calendario_aprobado_directo(payload) or _teams_aprobado_directo(payload)
    )
    if envio_directo is not None:
        _diag("acción aprobada ejecutada por la plataforma, sin llamar al modelo")
        _emit(envio_directo)
        return 0
    _activar_guardia(payload)
    adaptativo = bool(payload.get("thinking_adaptive"))
    if adaptativo:
        _activar_pensamiento_adaptativo()

    # ── Plugin discovery ────────────────────────────────────────
    # One-shot task subprocesses construct AIAgent directly and never enter a
    # hermes_cli entrypoint, so user plugins (including hermeshq_guard) are not
    # discovered and their hooks never register. Force discovery against the
    # agent home so policy enforcement and telemetry run in tasks too.
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered(force=True)
    except Exception as _plugin_err:  # noqa: BLE001  # plugins are best-effort in tasks
        _emit({"event": "warning", "warning": f"plugin discovery failed: {_plugin_err}"})
    overrides = payload.get("request_overrides") or {}
    _aplicar_overrides_resumen(overrides, adaptativo)
    _activar_busqueda_de_archivos()
    _preparar_exposicion(payload)
    _activar_integraciones_bajo_demanda(payload)

    # ── Auxiliary client env injection ─────────────────────────
    # When HermesHQ passes explicit api_key + base_url, the main agent
    # client is constructed directly. But the auxiliary clients (vision,
    # compression, etc.) resolve credentials independently via env vars.
    # Inject the agent's credentials so auxiliary tasks can use them too.
    _api_key = payload.get("api_key")
    _base_url = payload.get("base_url")
    if _api_key and _base_url:
        os.environ.setdefault("OPENAI_API_KEY", _api_key)
        os.environ.setdefault("OPENAI_BASE_URL", _base_url)
        for _task in ("vision", "compression", "session_search", "web_extract"):
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_API_KEY", _api_key)
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_BASE_URL", _base_url)
    # ── End auxiliary client env injection ─────────────────────

    # ── Attachment enrichment ──────────────────────────────────
    task_metadata = payload.get("metadata", {})
    attachments = task_metadata.get("attachments", [])
    if attachments:
        attachment_lines = []
        for att in attachments:
            att_path = att.get("path", "")
            if att_path:
                full_path = Path(payload["cwd"]) / att_path
                if full_path.exists():
                    line = f"- [{att.get('media_type', 'file')}] {att_path}"
                    if att.get("filename"):
                        line += f" (filename: {att['filename']})"
                    if att.get("caption"):
                        line += f" — {att['caption']}"
                    attachment_lines.append(line)
        if attachment_lines:
            payload["prompt"] += "\n\nAttached files:\n" + "\n".join(attachment_lines)
    # ── End attachment enrichment ──────────────────────────────

    agent = None
    try:
        from run_agent import AIAgent

        emitted_chunks: list[str] = []

        registro_tiempos = RegistroTiempos(payload["cwd"], str(payload["task_id"]))
        _REGISTRO["tiempos"] = registro_tiempos
        if (Path(payload["cwd"]) / ".hermes" / "recorte_contexto").exists():
            _activar_recorte_contexto(registro_tiempos)

        guardia = GuardiaDesbocada(payload.get("guardia_desbocada"))

        def on_razonamiento(texto) -> None:
            registro_tiempos.actividad("razonamiento_chars", len(texto or ""))
            if isinstance(texto, str):
                guardia.razonamiento(texto)

        def on_delta(delta) -> None:
            if guardia.disparo:
                return
            if delta is None:
                _emit({"event": "segment_break"})
                return
            if not isinstance(delta, str):
                return
            delta = limpiar_aviso_interno(delta)
            if not delta:
                return
            guardia.texto(delta)
            registro_tiempos.texto_visible()
            registro_tiempos.actividad("texto_chars", len(delta or ""))
            emitted_chunks.append(delta)
            _emit({"event": "delta", "data": delta})

        def on_tool_progress(status, name=None, *rest, duration=None, is_error=None, **kwargs) -> None:
            if status == "tool.started":
                guardia.herramientas()
                _HERRAMIENTAS_INICIADAS.append(str(name or "tool"))
                _emit({"event": "tool_started", "name": str(name or "tool")})
            if status != "tool.completed":
                return
            ms = int((duration or 0) * 1000)
            _DURACION_HERRAMIENTA_MS[str(name or "tool")] = ms
            registro_tiempos.herramienta(str(name or "tool"), ms, not bool(is_error))
            _emit(
                {
                    "event": "tool",
                    "name": str(name or "tool"),
                    "ok": not bool(is_error),
                    "ms": ms,
                }
            )

        def on_step(api_call_count, prev_tools=None) -> None:
            try:
                guardia.paso(int(api_call_count))
                registro_tiempos.paso_iniciado(int(api_call_count))
                _emit({"event": "step", "n": int(api_call_count)})
            except (TypeError, ValueError):
                pass

        agent = AIAgent(
            model=payload["model"],
            provider=payload.get("provider"),
            base_url=payload.get("base_url"),
            api_key=payload.get("api_key"),
            session_id=payload.get("session_id"),
            quiet_mode=True,
            enabled_toolsets=payload.get("enabled_toolsets") or None,
            disabled_toolsets=payload.get("disabled_toolsets") or None,
            ephemeral_system_prompt=payload.get("system_prompt"),
            request_overrides=payload.get("request_overrides") or None,
            max_iterations=payload.get("max_iterations", 90),
            skip_context_files=False,
            skip_memory=not _usa_memoria_persistente(payload),
            platform="hermeshq",
            stream_delta_callback=on_delta,
            tool_progress_callback=on_tool_progress,
            tool_complete_callback=_herramienta_terminada,
            step_callback=on_step,
            reasoning_callback=on_razonamiento,
            tool_gen_callback=registro_tiempos.generando_herramienta,
            status_callback=registro_tiempos.estado,
        )

        try:
            fijo = _fijo_estimado(agent, payload)
            cola = calibrar_compactacion(getattr(agent, "context_compressor", None), fijo)
            if cola is not None:
                _COMPACTACION.update({"fijo_estimado": fijo, "cola_protegida": cola})
                _diag(f"compactación calibrada: cola protegida {cola} tokens")
        except Exception as exc:  # noqa: BLE001
            _diag(f"no se pudo calibrar la compactación: {exc}")

        guardia.vincular(agent)
        fin_vigilancia = guardia.vigilar()
        try:
            result = agent.run_conversation(
                user_message=payload["prompt"],
                task_id=payload["task_id"],
                system_message=payload.get("system_override"),
                conversation_history=payload.get("conversation_history") or None,
            )
        finally:
            fin_vigilancia.set()

        registro_tiempos.fin()
        final_response, messages, tool_calls = _cerrar_turno(result, emitted_chunks)
        if guardia.disparo:
            final_response = guardia.aviso()
            messages = mensajes_sin_desborde(messages, final_response)
            tool_calls = _extract_tool_calls(messages)
            _emit({"event": "segment_break"})
            _emit({"event": "delta", "data": final_response})
        inicio_turno = _indice_ultimo_usuario(messages)
        tipo_narrado = None
        lecturas = None
        if not guardia.disparo and _guardia_accion_narrada(payload) and _llamadas_desde(messages, inicio_turno) == 0:
            tipo_narrado = (
                accion_narrada(_mensaje_del_usuario(payload), final_response)
                or confirmacion_narrada(
                    _mensaje_del_usuario(payload), final_response, _respuesta_anterior(messages, inicio_turno)
                )
                or confirmacion_con_intencion(
                    _mensaje_del_usuario(payload), final_response, _respuesta_anterior(messages, inicio_turno)
                )
            )
        elif not guardia.disparo and _guardia_accion_narrada(payload):
            lecturas = lecturas_del_turno(messages, inicio_turno)
            if lecturas:
                tipo_narrado = accion_narrada_tras_lecturas(_mensaje_del_usuario(payload), final_response)
            if not tipo_narrado:
                lecturas = None
        if (
            not tipo_narrado
            and not guardia.disparo
            and _guardia_accion_narrada(payload)
            and vista_calendario_a_mano(_mensaje_del_usuario(payload), final_response, messages, inicio_turno)
        ):
            tipo_narrado = "vista_calendario"
        if not tipo_narrado and not guardia.disparo and _guardia_accion_narrada(payload) and _chat_con_usuario(payload):
            _MAPA_IDS_CORREO.clear()
            _MAPA_IDS_CORREO.update(_mapa_ids_del_chat(payload))
            if incoherencia_de_correo(final_response, _MAPA_IDS_CORREO):
                tipo_narrado = "correo_incoherente"
        if not tipo_narrado and not guardia.disparo and _guardia_accion_narrada(payload):
            canal_envio = vista_envio_a_mano(_mensaje_del_usuario(payload), final_response, messages, inicio_turno)
            if canal_envio:
                tipo_narrado = f"vista_envio_{canal_envio}"
        if (
            not tipo_narrado
            and not guardia.disparo
            and _guardia_accion_narrada(payload)
            and respuesta_repetida_de_antes(_mensaje_del_usuario(payload), final_response, messages, inicio_turno)
        ):
            tipo_narrado = "repite"
        consulta = None
        if not tipo_narrado and not guardia.disparo and _guardia_accion_narrada(payload):
            consulta = consulta_sin_herramienta(
                _mensaje_del_usuario(payload),
                final_response,
                _nombres_llamados_desde(messages, inicio_turno),
                _texto_del_historial(payload.get("conversation_history")),
            )
            if not consulta and _chat_con_usuario(payload):
                consulta = cron_descrito_sin_consultar(
                    _mensaje_del_usuario(payload), final_response, _nombres_llamados_desde(messages, inicio_turno)
                )
            if consulta:
                tipo_narrado = "consulta"
        registro_narrada = None
        if tipo_narrado:
            _diag(f"acción narrada sin herramientas ({tipo_narrado}): se reintenta el turno una vez")
            registro_narrada = {
                "detectada": tipo_narrado,
                "reintento": True,
                "respuesta_descartada": final_response[:500],
            }
            if lecturas:
                registro_narrada["solo_lectura"] = lecturas
            llamadas_previas = result.get("api_calls")
            emitted_chunks.clear()
            _emit({"event": "segment_break"})
            fin_vigilancia = guardia.vigilar()
            try:
                nota = (
                    nota_consulta_sin_herramienta(*consulta)
                    if consulta
                    else _NOTA_REPITE.format(
                        mensaje=_mensaje_del_usuario(payload)[:300].replace("{", "(").replace("}", ")")
                    )
                    if tipo_narrado == "repite"
                    else nota_accion_narrada(tipo_narrado, registro_narrada["respuesta_descartada"], lecturas=lecturas)
                )
                pista = herramientas_del_pedido(_mensaje_del_usuario(payload), _herramientas_registradas(agent))
                if pista and tipo_narrado in ("edicion", "creacion", "subida"):
                    nota += f" Para esto tienes: {', '.join(pista)}."
                result = agent.run_conversation(
                    user_message=nota,
                    task_id=payload["task_id"],
                    system_message=payload.get("system_override"),
                    conversation_history=messages,
                )
            finally:
                fin_vigilancia.set()
            if isinstance(llamadas_previas, int) and isinstance(result.get("api_calls"), int):
                result = {**result, "api_calls": result["api_calls"] + llamadas_previas}
            final_response, messages, tool_calls = _cerrar_turno(result, emitted_chunks)
            if guardia.disparo:
                registro_narrada["resultado"] = "desbocada"
                final_response = guardia.aviso()
                messages = mensajes_sin_desborde(messages, final_response)
                tool_calls = _extract_tool_calls(messages)
                _emit({"event": "segment_break"})
                _emit({"event": "delta", "data": final_response})
            elif consulta:
                registro_narrada["fuente"] = consulta[0]
                if consulto_la_fuente(consulta[0], _nombres_llamados_desde(messages, inicio_turno)):
                    registro_narrada["resultado"] = "corregida"
                elif consulta_sin_herramienta(
                    _mensaje_del_usuario(payload),
                    final_response,
                    _nombres_llamados_desde(messages, inicio_turno),
                    _texto_del_historial(payload.get("conversation_history")),
                ) or (
                    consulta == _FUENTE_CRON
                    and cron_descrito_sin_consultar(
                        _mensaje_del_usuario(payload), final_response, _nombres_llamados_desde(messages, inicio_turno)
                    )
                ):
                    registro_narrada["resultado"] = "aviso_honesto"
                    final_response = aviso_consulta_sin_herramienta(consulta[0])
                    messages = list(messages) + [{"role": "assistant", "content": final_response}]
                else:
                    registro_narrada["resultado"] = "sin_afirmacion"
            elif tipo_narrado == "correo_incoherente":
                if incoherencia_de_correo(final_response, _MAPA_IDS_CORREO):
                    registro_narrada["resultado"] = "aviso_honesto"
                    final_response = _AVISO_CORREO_INCOHERENTE
                    messages = list(messages) + [{"role": "assistant", "content": final_response}]
                else:
                    registro_narrada["resultado"] = "corregida"
            elif tipo_narrado == "repite":
                if respuesta_repetida_de_antes(_mensaje_del_usuario(payload), final_response, messages, inicio_turno):
                    registro_narrada["resultado"] = "aviso_honesto"
                    final_response = _AVISO_REPITE
                    messages = list(messages) + [{"role": "assistant", "content": final_response}]
                else:
                    registro_narrada["resultado"] = "corregida"
            elif vista_calendario_a_mano(_mensaje_del_usuario(payload), final_response, messages, inicio_turno):
                registro_narrada["resultado"] = "aviso_honesto"
                final_response = _AVISO_VISTA_SIN_REGISTRAR
                messages = list(messages) + [{"role": "assistant", "content": final_response}]
            elif tipo_narrado.startswith("vista_envio_") and vista_envio_a_mano(
                _mensaje_del_usuario(payload), final_response, messages, inicio_turno
            ):
                registro_narrada["resultado"] = "aviso_honesto"
                final_response = _AVISO_VISTA_ENVIO_SIN_REGISTRAR
                messages = list(messages) + [{"role": "assistant", "content": final_response}]
            elif (
                _llamadas_desde(messages, inicio_turno) > 0
                if tipo_narrado == "busqueda"
                else _llamadas_con_efecto_desde(messages, inicio_turno) > 0
            ):
                registro_narrada["resultado"] = "corregida"
            elif tipo_narrado == "busqueda" and no_encontrado_sin_buscar(final_response):
                registro_narrada["resultado"] = "aviso_honesto"
                final_response = _AVISO_NEGATIVA_SIN_CONSULTA
                messages = list(messages) + [{"role": "assistant", "content": final_response}]
            elif sin_efecto_declarado(final_response, _llamadas_desde(messages, inicio_turno)):
                registro_narrada["resultado"] = "aviso_honesto"
                final_response = aviso_accion_no_hecha(
                    tipo_narrado,
                    registro_narrada["respuesta_descartada"],
                    solo_lectura=_llamadas_desde(messages, inicio_turno) > 0,
                    mensaje=_mensaje_del_usuario(payload),
                )
                messages = list(messages) + [{"role": "assistant", "content": final_response}]
            else:
                registro_narrada["resultado"] = "sin_afirmacion"

        registro_rechazo = None
        if not guardia.disparo and _guardia_accion_narrada(payload):
            registradas = _herramientas_registradas(agent)
            negadas = herramientas_negadas_disponibles(_mensaje_del_usuario(payload), final_response, registradas)
            usadas = set(_nombres_llamados_desde(messages, inicio_turno))
            if negadas and not usadas & set(negadas):
                _diag(f"rechazo falso de capacidad: {negadas} están registradas; se reintenta el turno una vez")
                registro_rechazo = {"disponibles": negadas, "respuesta_descartada": final_response[:500]}
                llamadas_previas = result.get("api_calls")
                emitted_chunks.clear()
                _emit({"event": "segment_break"})
                fin_vigilancia = guardia.vigilar()
                try:
                    result = agent.run_conversation(
                        user_message=nota_rechazo_falso(negadas),
                        task_id=payload["task_id"],
                        system_message=payload.get("system_override"),
                        conversation_history=messages,
                    )
                finally:
                    fin_vigilancia.set()
                if isinstance(llamadas_previas, int) and isinstance(result.get("api_calls"), int):
                    result = {**result, "api_calls": result["api_calls"] + llamadas_previas}
                final_response, messages, tool_calls = _cerrar_turno(result, emitted_chunks)
                if guardia.disparo:
                    registro_rechazo["resultado"] = "desbocada"
                    final_response = guardia.aviso()
                    messages = mensajes_sin_desborde(messages, final_response)
                    tool_calls = _extract_tool_calls(messages)
                    _emit({"event": "segment_break"})
                    _emit({"event": "delta", "data": final_response})
                elif set(_nombres_llamados_desde(messages, inicio_turno)) & set(negadas):
                    registro_rechazo["resultado"] = "corregida"
                elif herramientas_negadas_disponibles(_mensaje_del_usuario(payload), final_response, registradas):
                    registro_rechazo["resultado"] = "aviso_honesto"
                    final_response = AVISO_RECHAZO_FALSO
                    messages = list(messages) + [{"role": "assistant", "content": final_response}]
                else:
                    registro_rechazo["resultado"] = "sin_rechazo"

        registro_verificacion = None
        if not guardia.disparo and _guardia_accion_narrada(payload):
            metadata_tarea = payload.get("metadata") or {}
            parametros = {
                "cwd": os.getcwd(),
                "programada": bool(metadata_tarea.get("scheduled")),
                "mensaje": _mensaje_del_usuario(payload),
                "aprobado": bool(metadata_tarea.get("aprobacion_id") or metadata_tarea.get("aprobacion_aviso")),
            }
            final_response = rutas_media_corregidas(final_response, parametros["cwd"], messages, inicio_turno)
            sin_respaldo = verificar_afirmaciones(final_response, messages, inicio_turno, **parametros)
            if sin_respaldo:
                _diag(f"afirmaciones sin respaldo: {[p['tipo'] for p in sin_respaldo]}")
                registro_verificacion = {
                    "detectadas": sin_respaldo,
                    "respuesta_descartada": final_response[:500],
                    "reintento": not registro_narrada,
                }
                reintento = None
                if not registro_narrada:
                    llamadas_previas = result.get("api_calls")
                    emitted_chunks.clear()
                    _emit({"event": "segment_break"})
                    fin_vigilancia = guardia.vigilar()
                    try:
                        reintento = agent.run_conversation(
                            user_message=nota_verificacion(sin_respaldo),
                            task_id=payload["task_id"],
                            system_message=payload.get("system_override"),
                            conversation_history=messages,
                        )
                        cierre = _cerrar_turno(reintento, emitted_chunks)
                    except Exception as exc:  # noqa: BLE001
                        _diag(f"el reintento de la verificación falló: {exc}")
                        registro_verificacion["error_reintento"] = str(exc)[:300]
                        reintento = None
                    finally:
                        fin_vigilancia.set()
                if reintento is not None:
                    result = reintento
                    if isinstance(llamadas_previas, int) and isinstance(result.get("api_calls"), int):
                        result = {**result, "api_calls": result["api_calls"] + llamadas_previas}
                    final_response, messages, tool_calls = cierre
                    if guardia.disparo:
                        registro_verificacion["resultado"] = "desbocada"
                        final_response = guardia.aviso()
                        messages = mensajes_sin_desborde(messages, final_response)
                        tool_calls = _extract_tool_calls(messages)
                        _emit({"event": "segment_break"})
                        _emit({"event": "delta", "data": final_response})
                        sin_respaldo = []
                    else:
                        final_response = rutas_media_corregidas(
                            final_response, parametros["cwd"], messages, inicio_turno
                        )
                        sin_respaldo = verificar_afirmaciones(final_response, messages, inicio_turno, **parametros)
                        if not sin_respaldo:
                            registro_verificacion["resultado"] = "corregida"
                if sin_respaldo:
                    registro_verificacion["resultado"] = "aviso_honesto"
                    registro_verificacion["sin_respaldo_final"] = sin_respaldo
                    final_response = respuesta_verificada(final_response, sin_respaldo)
                    messages = list(messages) + [{"role": "assistant", "content": final_response}]
                    _emit({"event": "segment_break"})
                    _emit({"event": "delta", "data": final_response})

        if not guardia.disparo:
            final_response = adjunto_quitado_tras_subida(
                final_response, messages, inicio_turno, _mensaje_del_usuario(payload)
            )

        idioma_corregido = False
        if (
            not guardia.disparo
            and not registro_narrada
            and not (registro_verificacion and registro_verificacion.get("reintento"))
            and respuesta_en_ingles(final_response, _mensaje_del_usuario(payload))
        ):
            _diag("respuesta en inglés: se pide de nuevo en español")
            llamadas_previas = result.get("api_calls")
            emitted_chunks.clear()
            _emit({"event": "segment_break"})
            fin_vigilancia = guardia.vigilar()
            try:
                en_espanol = agent.run_conversation(
                    user_message=_NOTA_IDIOMA,
                    task_id=payload["task_id"],
                    system_message=payload.get("system_override"),
                    conversation_history=messages,
                )
                cierre = _cerrar_turno(en_espanol, emitted_chunks)
            except Exception as exc:  # noqa: BLE001
                _diag(f"el reintento en español falló: {exc}")
                en_espanol = None
            finally:
                fin_vigilancia.set()
            if en_espanol is not None and not guardia.disparo and not respuesta_en_ingles(cierre[0]):
                result = en_espanol
                if isinstance(llamadas_previas, int) and isinstance(result.get("api_calls"), int):
                    result = {**result, "api_calls": result["api_calls"] + llamadas_previas}
                final_response, messages, tool_calls = cierre
                idioma_corregido = True

        limpio = sin_parrafos_en_ingles(final_response)
        if limpio != final_response:
            _diag("se quitaron párrafos en inglés de la respuesta")
            final_response = limpio
        if (
            (registro_narrada and registro_narrada.get("reintento"))
            or (registro_verificacion and registro_verificacion.get("reintento"))
            or idioma_corregido
        ):
            sin_disculpa = quitar_disculpa_por_revision(final_response)
            if sin_disculpa != final_response:
                _diag("se quitó una disculpa por la revisión interna")
                final_response = sin_disculpa
        convertido = _borrador_a_vista_previa(payload, final_response, messages)
        if convertido:
            _diag("borrador escrito a mano convertido en vista previa del servidor")
            final_response, messages = convertido
            tool_calls = _extract_tool_calls(messages)
        elif not guardia.disparo and _chat_con_usuario(payload):
            vista_servidor = vista_calendario_del_servidor(
                messages, inicio_turno, _mensaje_del_usuario(payload)
            ) or vista_teams_del_servidor(messages, inicio_turno)
            if vista_servidor:
                _diag("vista previa tomada de lo que registró el servidor")
                final_response = vista_servidor
                messages = list(messages) + [{"role": "assistant", "content": final_response}]
                _marcar_vista_mostrada(payload, vista_servidor)
        _diag("emitiendo el resultado final")
        resultado = build_result_payload(result, final_response, messages, tool_calls)
        if registro_narrada:
            resultado["accion_narrada"] = registro_narrada
        if registro_rechazo:
            resultado.setdefault("accion_narrada", {"detectada": "rechazo_falso"})["rechazo_falso"] = registro_rechazo
        if registro_verificacion:
            resultado["verificacion"] = registro_verificacion
        if guardia.disparo:
            resultado["generacion_desbocada"] = guardia.disparo
        if _REGISTRO.get("correcciones"):
            resultado["correcciones_aplicadas"] = _REGISTRO["correcciones"]
        if idioma_corregido:
            resultado["idioma_corregido"] = True
        _emit(resultado)
        return 0
    except Exception as exc:  # noqa: BLE001  # CLI top-level catch-all for error reporting
        error_event = {
            "event": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }
        if agent is not None:
            usage = _usage_from_agent(agent)
            if usage:
                error_event["usage"] = usage
        _emit(error_event)
        return 1


if __name__ == "__main__":
    sys.exit(main())
