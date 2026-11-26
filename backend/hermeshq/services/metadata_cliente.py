from __future__ import annotations

import re
from typing import Any

MAX_TITULO = 512
MAX_ID = 64
MAX_ADJUNTOS = 20
MAX_TEXTO_ADJUNTO = 255
FUENTES_CLIENTE = frozenset({"agent_conversation", "chat", "web", "mobile_app"})
DESTINOS_RESPUESTA_CLIENTE = frozenset({"mobile_app"})
CLAVES_PERMITIDAS = frozenset({"conversation", "source", "reply_to", "title", "thread_id", "attachments"})
_RUTA_ADJUNTO = re.compile(r"^uploads/[0-9a-fA-F-]{36}\.[A-Za-z0-9]{1,10}$")
_CLAVES_ADJUNTO = ("media_type", "filename", "caption")


def _texto(valor: Any, maximo: int) -> str | None:
    if not isinstance(valor, str):
        return None
    limpio = valor.strip()
    return limpio[:maximo] if limpio else None


def _adjunto(valor: Any) -> dict[str, str] | None:
    if not isinstance(valor, dict):
        return None
    ruta = _texto(valor.get("path"), MAX_TEXTO_ADJUNTO)
    if not ruta or not _RUTA_ADJUNTO.match(ruta):
        return None
    resultado = {"path": ruta}
    for clave in _CLAVES_ADJUNTO:
        dato = _texto(valor.get(clave), MAX_TEXTO_ADJUNTO)
        if dato:
            resultado[clave] = dato
    return resultado


def metadata_cliente_segura(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    seguro: dict[str, Any] = {}
    if metadata.get("conversation") is True:
        seguro["conversation"] = True
    fuente = _texto(metadata.get("source"), 64)
    if fuente in FUENTES_CLIENTE:
        seguro["source"] = fuente
    destino = _texto(metadata.get("reply_to"), 64)
    if destino and destino.lower() in DESTINOS_RESPUESTA_CLIENTE:
        seguro["reply_to"] = destino.lower()
    titulo = _texto(metadata.get("title"), MAX_TITULO)
    if titulo:
        seguro["title"] = titulo
    hilo = _texto(metadata.get("thread_id"), MAX_ID)
    if hilo:
        seguro["thread_id"] = hilo
    adjuntos_crudos = metadata.get("attachments")
    if isinstance(adjuntos_crudos, list):
        adjuntos = [a for a in (_adjunto(item) for item in adjuntos_crudos[:MAX_ADJUNTOS]) if a]
        if adjuntos:
            seguro["attachments"] = adjuntos
    return seguro
