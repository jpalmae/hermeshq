from __future__ import annotations

from typing import Any

MAX_TITULO = 512
MAX_ID = 64
CLAVES_PERMITIDAS = frozenset({"title", "parent_task_id", "callback_delivery"})
_CLAVES_CALLBACK = ("platform", "chat_id", "thread_id")


def _texto(valor: Any, maximo: int) -> str | None:
    if not isinstance(valor, str):
        return None
    limpio = valor.strip()
    return limpio[:maximo] if limpio else None


def _callback(valor: Any) -> dict[str, str | None] | None:
    if not isinstance(valor, dict):
        return None
    resultado: dict[str, str | None] = {}
    for clave in _CLAVES_CALLBACK:
        dato = valor.get(clave)
        if isinstance(dato, int) and not isinstance(dato, bool):
            dato = str(dato)
        resultado[clave] = _texto(dato, 255)
    if not resultado["platform"] or not resultado["chat_id"]:
        return None
    return resultado


def metadata_delegacion_segura(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    seguro: dict[str, Any] = {}
    titulo = _texto(metadata.get("title"), MAX_TITULO)
    if titulo:
        seguro["title"] = titulo
    padre = _texto(metadata.get("parent_task_id"), MAX_ID)
    if padre:
        seguro["parent_task_id"] = padre
    callback = _callback(metadata.get("callback_delivery"))
    if callback:
        seguro["callback_delivery"] = callback
    return seguro
