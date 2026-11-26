from __future__ import annotations

import re
import unicodedata

from sqlalchemy.ext.asyncio import AsyncSession

AVISO_FALTA_ADJUNTO = (
    "📎 Para hacerlo necesito {objeto} que mencionas, y no me llegó en este mensaje. Envíala por aquí y empiezo con "
    "ella; no hace falta repetir el pedido."
)
_NOTA_ADJUNTOS = re.compile(r"^\s*\[Archivos adjuntos[^\]]*\]\s*", re.S)
_MENCION = re.compile(
    r"\b(?:la|esta|esa|mi|las|estas|esas|mis)\s+(?P<obj>imagen(?:es)?|captura(?:s)?(?: de pantalla)?|fotos?|"
    r"fotografias?|pantallazos?|screenshots?)\b|\b(?:el|este|ese)\s+(?P<adj>adjunto|archivo adjunto|pdf adjunto)\b"
)
_UBICADA = re.compile(
    r"^\s*(?:que (?:esta|tengo|guarde|subi)|(?:en|de|del) (?:mi |el |la )?(?:onedrive|sharepoint|carpeta|correo)|"
    r"corporativa|de (?:la )?marca|institucional|visual)"
)
_LETRAS_MAXIMAS_SOLO_ADJUNTO = 40
_ENTREGA_ANTES = re.compile(
    r"\b(?:avisame|avisarme|enviame|mandame|entregame|devuelveme|dejame|respondeme|pasame|adjuntame|compartime)\s+"
    r"(?:(?:aqui|por aqui|por este chat|por el chat|por teams|de vuelta)\s+)?(?:con\s+)?$"
)
_ENTREGA_DESPUES = re.compile(r"^\s*(?:cuando|al)\s+(?:termin|acab|finaliz|est[ea] list|lo tengas)")


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texto).lower()


def tiene_adjunto(contenido: str) -> bool:
    return bool(_NOTA_ADJUNTOS.match(contenido or ""))


def menciona_adjunto_ausente(contenido: str) -> str | None:
    if tiene_adjunto(contenido):
        return None
    texto = _normalizar(contenido)
    for coincidencia in _MENCION.finditer(texto):
        if _UBICADA.match(texto[coincidencia.end() : coincidencia.end() + 60]):
            continue
        if _ENTREGA_ANTES.search(texto[max(0, coincidencia.start() - 80) : coincidencia.start()]) or (
            _ENTREGA_DESPUES.match(texto[coincidencia.end() : coincidencia.end() + 40])
        ):
            continue
        objeto = coincidencia.group("obj") or ""
        return "el archivo adjunto" if not objeto or objeto.startswith("adjunt") else "la imagen"
    return None


def separar_adjunto(contenido: str) -> tuple[str, str]:
    coincidencia = _NOTA_ADJUNTOS.match(contenido or "")
    if not coincidencia:
        return "", contenido or ""
    return coincidencia.group(0).strip(), (contenido or "")[coincidencia.end() :].strip()


def es_solo_adjunto(contenido: str) -> bool:
    nota, resto = separar_adjunto(contenido)
    return bool(nota) and sum(c.isalnum() for c in resto) <= _LETRAS_MAXIMAS_SOLO_ADJUNTO


def aviso_falta_adjunto(objeto: str) -> str:
    return AVISO_FALTA_ADJUNTO.format(objeto=objeto)


def _es_aviso(texto: str) -> bool:
    return (texto or "").startswith("📎 Para hacerlo necesito ")


async def pedido_retenido(db: AsyncSession, session_id: str | None) -> str | None:
    from hermeshq.services.auto_background import _load_recent_messages

    mensajes = await _load_recent_messages(db, session_id)
    for indice, mensaje in enumerate(mensajes):
        if mensaje.role != "assistant":
            continue
        if not _es_aviso(mensaje.content or ""):
            return None
        anterior = next((m for m in mensajes[indice + 1 :] if m.role == "user"), None)
        return (anterior.content or "").strip() or None if anterior is not None else None
    return None


def unir_con_retenido(contenido: str, retenido: str) -> str:
    nota, resto = separar_adjunto(contenido)
    partes = [nota, retenido] + ([resto] if resto else [])
    return "\n".join(p for p in partes if p)
