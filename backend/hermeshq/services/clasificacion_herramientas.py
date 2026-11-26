from __future__ import annotations

import json
import re

OK = "ok"
VISTA_PREVIA = "vista_previa"
RECHAZO_GUARDIA = "rechazo_guardia"
ERROR_EXTERNO = "error_externo"
ERROR_INTERNO = "error_interno"
SIN_RESULTADO = "sin_resultado"
CLASES = (OK, VISTA_PREVIA, RECHAZO_GUARDIA, ERROR_EXTERNO, ERROR_INTERNO, SIN_RESULTADO)
FALLAS = (ERROR_EXTERNO, ERROR_INTERNO, SIN_RESULTADO)

_AVISO_DE_BUCLE = re.compile(r"\s*\[Tool loop warning:.*$", re.S)
_INEXISTENTE = re.compile(r"^Tool '[^']*' does not exist")
_EXTERNO = re.compile(
    r"graph|itemnotfound|resourcenotfound|accessdenied|mailboxnotenabled|throttl|unauthorized|forbidden|"
    r"\bhttp\s*[45]\d\d\b|\b(?:401|403|404|409|423|429|500|502|503|504)\b|timed? ?out|timeout|"
    r"connection|conexi[oó]n|expir|token|quota|cuota|no se pudo (?:conectar|obtener|leer el servidor)|"
    r"servidor de microsoft|microsoft 365 (?:no|respondi)|sharepoint (?:no|respondi)|dns|ssl|certificat",
    re.I,
)
_TRAZA = re.compile(r'File ".*", line \d+|\^{3,}')
_INTERNO = re.compile(
    r"traceback|keyerror|typeerror|attributeerror|valueerror|nameerror|indexerror|jsondecodeerror|exception|"
    r"generation failed|no se encontr[oó] el generador|is not installed|error interno|internal error|"
    r"no se pudo generar|runtime process failed|tool execution failed",
    re.I,
)


def _como_json(texto: str):
    texto = _AVISO_DE_BUCLE.sub("", texto).strip()
    if not texto.startswith("{"):
        return None
    try:
        return json.loads(texto)
    except ValueError:
        return None


def clasificar(nombre: str, resultado, es_error: bool = False) -> str:
    if resultado is None:
        return SIN_RESULTADO
    texto = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False, default=str)
    if not texto.strip():
        return SIN_RESULTADO
    if _INEXISTENTE.match(texto.strip()):
        return ERROR_INTERNO
    datos = _como_json(texto)
    if not isinstance(datos, dict):
        if es_error or re.match(r"\s*(?:error\b|traceback)", texto, re.I):
            return ERROR_INTERNO if _INTERNO.search(texto) or not _EXTERNO.search(texto) else ERROR_EXTERNO
        return OK
    if datos.get("requiere_aprobacion") or datos.get("vista_previa"):
        return VISTA_PREVIA
    exito = datos.get("success", datos.get("ok"))
    if datos.get("status") == "error":
        exito = False
    if exito is True or (exito is None and not datos.get("error") and not es_error):
        return OK
    error = str(datos.get("error") or datos.get("output") or datos.get("problemas") or texto)
    if _TRAZA.search(error) or _INTERNO.search(error):
        return ERROR_INTERNO
    if _EXTERNO.search(error):
        return ERROR_EXTERNO
    return RECHAZO_GUARDIA
