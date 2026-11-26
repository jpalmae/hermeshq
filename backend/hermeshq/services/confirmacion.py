import re

from hermeshq.services.respuesta_corta import pedido_original

_CONFIRMACION = re.compile(
    r"^\W*(s[ií]|ok|okay|dale|listo|confirmo|confirmado|adelante|hazlo|apruebo|aprobado|env[ií]a(lo|la)?|reenv[ií]a(lo|la)?|resp[oó]nde(lo|le)?|contesta(lo|le)?|"
    r"m[aá]nda(lo|la)?|eliminar\s+\w{4,8})(\W+(s[ií]|por favor|gracias|env[ií]a(lo|la)?|reenv[ií]a(lo|la)?|resp[oó]nde(lo|le)?|contesta(lo|le)?|dale|con todo))*\W*$",
    re.IGNORECASE,
)


def es_confirmacion(task) -> bool:
    metadata = getattr(task, "metadata_json", None) or {}
    texto = str(metadata.get("mensaje_usuario") or pedido_original(getattr(task, "prompt", "") or ""))
    return bool(_CONFIRMACION.match(texto.strip()))


_PIDE_APROBACION = re.compile(
    r"¿lo (re)?env[ií]o|¿la env[ií]o|vista previa|responde en el chat exactamente|¿confirmas|¿lo (creo|agendo|elimino|"
    r"borro|comparto|subo)\?|¿la (creo|agendo|elimino|borro|muevo|cancelo)\?",
    re.IGNORECASE,
)


def confirma_una_aprobacion(task, mensajes) -> bool:
    if not es_confirmacion(task):
        return False
    anterior = next(
        (str(m.content or "") for m in reversed(list(mensajes or [])) if getattr(m, "role", "") == "assistant"), ""
    )
    return bool(_PIDE_APROBACION.search(anterior))
