from __future__ import annotations

import json
import os
import re
import unicodedata


def _sin_acentos(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor if valor is not None else "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto.lower()).strip()


_NUMEROS_PALABRA = {
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
}
_LAMINAS_PEDIDAS = re.compile(
    r"\b(de|con|en|solo|maximo|hasta|menos|minimo|haz|hazme|arma|armame|crea|creame|genera|generame)\s+(?:exactamente\s+|solamente\s+|justo\s+)?("
    r"\d{1,2}|" + "|".join(_NUMEROS_PALABRA) + r")\s+(?:diapositivas|laminas|slides)\b([^.\n]*)"
)
_ESTRUCTURA_EN_LA_LISTA = re.compile(r"\b(portada|agenda|indice|cierre)\b")
_SOLO_CONTENIDO = re.compile(r"^\s*de contenido\b|\bsin (?:contar|incluir)\b|\bademas de\b|\bmas (?:la )?portada\b")
_REFERENCIA_A_OTRA = {"la", "el", "las", "los", "esa", "esta", "esas", "estas", "aquella", "plantilla"}
_COMO_MAXIMO = {"maximo", "hasta"}
_COMO_MINIMO = {"menos", "minimo"}
_TURNO_DEL_AGENTE = re.compile(r"\s*(?:Tú|Tu|Asistente|Agente):")
_NOTA_AL_INICIO = re.compile(r"^\s*\[[^\]]*\]\s*")
_MODELOS_DE_ESTRUCTURA = ("portada", "divisor", "cierre", "agenda")
_TITULOS_DE_ESTRUCTURA = re.compile(r"^\s*(agenda|consultas|preguntas|gracias)\b")
_CLAVES_DE_CONTENIDO = ("visual", "resumen", "panel", "items", "puntos", "texto", "tabla")


def _laminas_pedidas() -> tuple[int, str] | None:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for texto in (metadata.get("mensaje_usuario"), metadata.get("pedido_del_usuario"), payload.get("pedido_anterior")):
        texto = str(texto or "")
        while _NOTA_AL_INICIO.match(texto):
            texto = _NOTA_AL_INICIO.sub("", texto, count=1)
        texto = "\n".join(linea for linea in texto.splitlines() if not _TURNO_DEL_AGENTE.match(linea))
        plano = _sin_acentos(texto).lower()
        for encontrado in _LAMINAS_PEDIDAS.finditer(plano):
            previa = plano[: encontrado.start()].split()[-1:]
            if encontrado.group(1) == "de" and previa and previa[0] in _REFERENCIA_A_OTRA:
                continue
            valor = encontrado.group(2)
            numero = int(valor) if valor.isdigit() else _NUMEROS_PALABRA[valor]
            resto = encontrado.group(3)
            if not _SOLO_CONTENIDO.search(resto):
                numero -= len(set(_ESTRUCTURA_EN_LA_LISTA.findall(resto)))
            modo = (
                "maximo"
                if encontrado.group(1) in _COMO_MAXIMO
                else "minimo"
                if encontrado.group(1) in _COMO_MINIMO
                else "exacto"
            )
            return numero, texto[max(0, encontrado.start() - 40) : encontrado.end()].strip(), modo
    return None
