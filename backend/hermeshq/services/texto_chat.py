import re

LINEA_MEDIA = re.compile(r"^\s*MEDIA:\s*\S.*$", re.MULTILINE)

SEPARADOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _celdas(linea: str) -> list[str]:
    return [celda.strip() for celda in linea.strip().strip("|").split("|")]


def _es_fila_de_tabla(linea: str) -> bool:
    return linea.count("|") >= 2 and bool(linea.strip())


def _fila(celdas: list[str]) -> str:
    return "| " + " | ".join(celdas) + " |"


def _tabla_markdown(encabezados: list[str], filas: list[list[str]]) -> list[str]:
    columnas = len(encabezados)
    lineas = [_fila(encabezados), "|" + "|".join(["---"] * columnas) + "|"]
    for fila in filas:
        fila = (fila + [""] * columnas)[:columnas]
        lineas.append(_fila(fila))
    return lineas


ITEM_DE_LISTA = re.compile(r"^\s*([-*•+]|\d+[.)])\s")


def _separar_lineas(texto: str) -> str:
    lineas = texto.split("\n")
    salida: list[str] = []
    en_codigo = False
    for i, linea in enumerate(lineas):
        if linea.strip().startswith("```"):
            en_codigo = not en_codigo
        salida.append(linea)
        if en_codigo or i + 1 >= len(lineas):
            continue
        siguiente = lineas[i + 1]
        if not linea.strip() or not siguiente.strip():
            continue
        if _es_fila_de_tabla(linea) or _es_fila_de_tabla(siguiente):
            continue
        if ITEM_DE_LISTA.match(linea) and ITEM_DE_LISTA.match(siguiente):
            continue
        if linea.strip().startswith("```") or siguiente.strip().startswith("```"):
            continue
        salida.append("")
    return "\n".join(salida)


def tablas_para_teams(texto: str | None) -> str:
    if not texto:
        return ""
    return _separar_lineas(_normalizar_tablas(texto))


def _normalizar_tablas(texto: str) -> str:
    if "|" not in texto:
        return texto
    lineas = texto.split("\n")
    salida: list[str] = []
    i = 0
    while i < len(lineas):
        if i + 1 < len(lineas) and _es_fila_de_tabla(lineas[i]) and SEPARADOR.match(lineas[i + 1]):
            encabezados = _celdas(lineas[i])
            i += 2
            filas: list[list[str]] = []
            while i < len(lineas) and _es_fila_de_tabla(lineas[i]):
                filas.append(_celdas(lineas[i]))
                i += 1
            if salida and salida[-1].strip():
                salida.append("")
            salida.extend(_tabla_markdown(encabezados, filas))
            if i < len(lineas) and lineas[i].strip():
                salida.append("")
            continue
        salida.append(lineas[i])
        i += 1
    return "\n".join(salida)


def quitar_lineas_media(texto: str | None) -> str:
    if not texto:
        return texto or ""
    limpio = LINEA_MEDIA.sub("", texto)
    return re.sub(r"\n{3,}", "\n\n", limpio).strip()


PREFIJO_CLAVE_TEAMS = "bridge-teams"


def respuesta_para_canal(texto: str | None, es_teams: bool) -> str:
    if not es_teams:
        return texto or ""
    return tablas_para_teams(texto)
