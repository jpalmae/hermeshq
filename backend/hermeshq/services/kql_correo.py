from __future__ import annotations

import datetime as _dt
import re

MAX_TERMINOS = 12
_ALFABETO = r"[\wáéíóúñüÁÉÍÓÚÑÜ.\-@]"
_PUNTUACION_EN_FRASE = r"[:'’°/,;#&()¿?¡!+%ºª]"
_VALOR_COMILLAS = re.compile(rf'"((?:{_ALFABETO}|{_PUNTUACION_EN_FRASE}|[ \t]){{1,100}})"')
_VALOR_TOKEN = re.compile(rf"({_ALFABETO}{{1,60}})")
_FECHA = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class KqlNoPermitido(ValueError):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def _valor(texto: str, pos: int) -> tuple[str, int]:
    if pos < len(texto) and texto[pos] == '"':
        m = _VALOR_COMILLAS.match(texto, pos)
        if not m:
            raise KqlNoPermitido("valor_no_permitido")
        return m.group(1).strip(), m.end()
    m = _VALOR_TOKEN.match(texto, pos)
    if not m:
        raise KqlNoPermitido("valor_no_permitido")
    return m.group(1), m.end()


def validar(texto: str) -> str:
    if not isinstance(texto, str) or not texto.strip():
        raise KqlNoPermitido("consulta_vacia")
    if len(texto) > 400:
        raise KqlNoPermitido("consulta_demasiado_larga")
    pos, n, terminos = 0, len(texto), []
    while pos < n:
        if texto[pos] == " ":
            pos += 1
            continue
        if len(terminos) >= MAX_TERMINOS:
            raise KqlNoPermitido("demasiados_terminos")
        m = re.match(r"(from|subject|to|cc):", texto[pos:], re.I)
        if m:
            campo = m.group(1).lower()
            valor, pos = _valor(texto, pos + m.end())
            if not valor:
                raise KqlNoPermitido("valor_vacio")
            terminos.append(f'{campo}:"{valor}"')
            continue
        m = re.match(r"received(>=|<=)", texto[pos:], re.I)
        if m:
            fm = _FECHA.match(texto, pos + m.end())
            if not fm:
                raise KqlNoPermitido("fecha_no_valida")
            try:
                fecha = _dt.date(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)))
            except ValueError:
                raise KqlNoPermitido("fecha_no_valida")
            terminos.append(f"received{m.group(1)}{fecha.isoformat()}")
            pos = fm.end()
            continue
        m = re.match(r"hasattachments:true(?=\s|$)", texto[pos:], re.I)
        if m:
            terminos.append("hasattachments:true")
            pos += m.end()
            continue
        if texto[pos] == '"':
            frase, fin = _valor(texto, pos)
            if not frase or (fin < n and texto[fin] != " "):
                raise KqlNoPermitido("valor_no_permitido")
            terminos.append(f'"{frase}"')
            pos = fin
            continue
        m = re.match(rf"({_ALFABETO}{{2,50}})(?=\s|$)", texto[pos:])
        if m:
            palabra = m.group(1)
            if palabra.upper() in ("OR", "AND", "NOT"):
                raise KqlNoPermitido("operador_no_permitido")
            terminos.append(palabra)
            pos += m.end()
            continue
        if re.match(r"(OR|AND|NOT)(?=\s|$)", texto[pos:]):
            raise KqlNoPermitido("operador_no_permitido")
        raise KqlNoPermitido("termino_no_permitido")
    return " ".join(terminos)
