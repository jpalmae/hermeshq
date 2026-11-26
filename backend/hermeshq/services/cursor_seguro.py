from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

DURACION_SEGUNDOS = 15 * 60
MAX_OFFSET = 5000


class CursorNoValido(ValueError):
    pass


def _firma(secreto: str, cuerpo: bytes) -> str:
    return hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def _vinculo(contexto: dict, carpeta: str, consulta: str, top: int) -> dict:
    return {
        "u": str(contexto.get("usuario", "")),
        "a": str(contexto.get("agente", "")),
        "t": str(contexto.get("tarea", "")),
        "c": carpeta,
        "q": hashlib.sha256(consulta.encode("utf-8")).hexdigest(),
        "n": int(top),
    }


def emitir(
    secreto: str, contexto: dict, carpeta: str, consulta: str, top: int, offset: int, ahora: float | None = None
) -> str:
    datos = {
        **_vinculo(contexto, carpeta, consulta, top),
        "o": int(offset),
        "e": int((ahora or time.time()) + DURACION_SEGUNDOS),
    }
    cuerpo = json.dumps(datos, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sello = _firma(secreto, cuerpo)
    return base64.urlsafe_b64encode(cuerpo).decode("ascii") + "." + sello


def validar(
    secreto: str, cursor: str, contexto: dict, carpeta: str, consulta: str, top: int, ahora: float | None = None
) -> int:
    try:
        cuerpo_b64, sello = cursor.split(".", 1)
        cuerpo = base64.urlsafe_b64decode(cuerpo_b64.encode("ascii"))
    except (ValueError, UnicodeEncodeError, base64.binascii.Error) as exc:
        raise CursorNoValido("formato") from exc
    if not hmac.compare_digest(sello, _firma(secreto, cuerpo)):
        raise CursorNoValido("firma")
    try:
        datos = json.loads(cuerpo)
    except ValueError as exc:
        raise CursorNoValido("contenido") from exc
    if datos.get("e", 0) < (ahora or time.time()):
        raise CursorNoValido("caducado")
    esperado = _vinculo(contexto, carpeta, consulta, top)
    if any(datos.get(k) != v for k, v in esperado.items()):
        raise CursorNoValido("otro_contexto")
    offset = datos.get("o")
    if not isinstance(offset, int) or not 0 < offset <= MAX_OFFSET:
        raise CursorNoValido("offset")
    return offset
