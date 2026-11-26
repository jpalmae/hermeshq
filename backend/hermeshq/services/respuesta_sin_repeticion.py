from __future__ import annotations

import re
import unicodedata

ACLARACION_ENTRADA_VACIA = "No recibí una instrucción clara. ¿Qué necesitas que haga?"
ACLARACION_REPETICION = (
    "Esa es la misma respuesta que ya te di. ¿Quieres que la reformule, que amplíe algo o que haga otra cosa?"
)

AVISO_SIN_RESPUESTA = (
    "⚠️ No logré armar una respuesta esta vez: las herramientas fallaron o no devolvieron un resultado, así que no "
    "quedó nada hecho. Pídemelo de nuevo; si vuelve a pasar, dime qué necesitas y lo intento de otra forma."
)

_SUFIJO_PLATAFORMA = re.compile(r"\n\n\[.*$", re.S)
_ETIQUETA_PLATAFORMA = re.compile(r"^\[(?:Mensaje anterior|Solicitud anterior)[^\]]*\]\s*", re.I)
_PIDE_REPETIR = re.compile(r"\b(repite|repetir|otra vez|de nuevo|lo mismo)\b", re.I)
_MINIMO_NORMALIZADO = 2
_AVISO_DE_FALLO = re.compile(
    r"^\s*(?:⚠️?|❌|⏹️?)|\bno (?:pude|logr[eé]|alcanc[eé]|fue posible|se pudo)\b|\bse cort[oó]\b", re.I
)


_VISTA_PREVIA_FIJA = re.compile(r"\*\*Vista previa [^*]*\*\*[\s\S]*Responde «sí» para confirmar\.\s*$")


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    texto = _SUFIJO_PLATAFORMA.sub("", texto)
    texto = _ETIQUETA_PLATAFORMA.sub("", texto)
    return re.sub(r"\s+", " ", texto).strip().lower()


def _letras_o_digitos(texto: str) -> int:
    return sum(1 for c in texto if c.isalnum())


def _es_respuesta_larga(respuesta: str) -> bool:
    return len(_normalizar(respuesta)) >= 40


def es_entrada_vacia(prompt: str) -> bool:
    return _letras_o_digitos(_normalizar(prompt)) < _MINIMO_NORMALIZADO


def _ultima_respuesta_anterior(historial: list[dict] | None) -> str:
    for mensaje in reversed(historial or []):
        if isinstance(mensaje, dict) and mensaje.get("role") == "assistant":
            contenido = mensaje.get("content")
            if isinstance(contenido, str) and contenido.strip():
                return contenido
    return ""


def _ultimo_pedido_anterior(historial: list[dict] | None) -> str:
    for mensaje in reversed(historial or []):
        if isinstance(mensaje, dict) and mensaje.get("role") == "user":
            contenido = mensaje.get("content")
            if isinstance(contenido, str) and contenido.strip():
                return contenido
    return ""


def _palabras(texto: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9]+", _normalizar(texto)) if len(p) > 2}


def repite_el_pedido(prompt: str, historial: list[dict] | None) -> bool:
    actual, anterior = _palabras(prompt), _palabras(_ultimo_pedido_anterior(historial))
    if not actual or not anterior:
        return False
    return len(actual & anterior) / len(actual | anterior) >= 0.6


def es_repeticion(respuesta: str, historial: list[dict] | None) -> bool:
    anterior = _normalizar(_ultima_respuesta_anterior(historial))
    actual = _normalizar(respuesta)
    if not anterior or not actual:
        return False
    return (
        actual == anterior or (len(actual) >= 40 and actual in anterior) or (len(anterior) >= 40 and anterior in actual)
    )


def filtrar_respuesta(prompt: str, respuesta: str, historial: list[dict] | None) -> str:
    if _PIDE_REPETIR.search(prompt or ""):
        return respuesta
    if not (respuesta or "").strip():
        return ACLARACION_ENTRADA_VACIA if es_entrada_vacia(prompt) else AVISO_SIN_RESPUESTA
    if _AVISO_DE_FALLO.search(respuesta) or _VISTA_PREVIA_FIJA.search(respuesta):
        return respuesta
    if es_entrada_vacia(prompt) and es_repeticion(respuesta, historial):
        return ACLARACION_ENTRADA_VACIA
    if (
        es_repeticion(respuesta, historial)
        and not es_entrada_vacia(prompt)
        and _es_respuesta_larga(respuesta)
        and not repite_el_pedido(prompt, historial)
    ):
        return ACLARACION_REPETICION
    return respuesta
