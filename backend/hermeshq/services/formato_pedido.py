import re

_EXCEL = re.compile(r"\b(excel|xlsx|hoja de calculo|planilla)\b")
_HTML = re.compile(
    r"\b(html|dashboards?|tableros?|panel de control|pagina web|(?:informes?|reportes?)\s+interactivos?)\b"
)
_WORD = re.compile(r"\b(word|docx)\b")
_PDF = re.compile(r"\bpdf\b")

_SIN_TILDES = str.maketrans("áéíóúüÁÉÍÓÚÜ", "aeiouuAEIOUU")

_DET_LISTA = "el|la|los|las|este|ese|esta|esa|estos|esos|estas|esas|aquel|aquella|mi|mis|tu|tus|su|sus|nuestro|nuestra|dicho|dicha"
_DET = f"(?:{_DET_LISTA})"
_DEMOSTRATIVO = (
    r"(?:este|ese|esta|esa|estos|esos|estas|esas|aquel|aquella|mi|mis|tu|tus|su|sus|mismo|misma|dicho|dicha)"
)
_VERBOS_LECTURA = (
    r"(?:lee|leer|leas|abre|abrir|abras|revisa|revisar|revises|analiza|analizar|analices|mira|mirar|toma|tomar"
    r"|usa|usar|uses|utiliza|utilizar|ocupa|ocupar|procesa|procesar|resume|resumir|resumeme|considera|carga|cargar"
    r"|extrae|extraer|convierte|convertir|conviertas|pasa|pasar|pases|exporta|exportar|transforma|transformar"
    r"|traduce|traducir|corrige|corregir|actualiza|actualizar|edita|editar|modifica|modificar|completa|completar)"
)
_PREPOSICIONES = (
    r"(?:desde|con|en|usando|utilizando|segun|sobre|mediante|basado en|basada en|basandote en|basate en|tomando"
    r"|a partir de)"
)
_INTERMEDIO = r"(?:(?:archivo|documento|fichero|libro|mismo|misma)\s+(?:de\s+|en\s+)?)?"
_PREVIO_FUENTE = re.compile(
    r"(?:\bdel|\bde\s+(?:un|una|"
    + _DET_LISTA
    + r")|\b"
    + _PREPOSICIONES
    + r"\s+"
    + _DET
    + r"|\b"
    + _VERBOS_LECTURA
    + r"\s+"
    + _DET
    + r"|\b"
    + _DEMOSTRATIVO
    + r")\s+"
    + _INTERMEDIO
    + r"$"
)
_PREVIO_DEFINIDO = re.compile(r"\b(?:el|la|los|las|al)\s+" + _INTERMEDIO + r"$")
_VERBOS_PASADO = (
    r"(?:mande|mandaste|mando|envie|enviaste|envio|adjunte|adjuntaste|acabas|acabo|acabamos|hiciste|hice|hizo"
    r"|hicimos|generaste|genere|genero|creaste|cree|subi|subiste|pase|pasaste|comparti|compartiste|deje|dejaste"
    r"|diste|di|tienes|tengo|tenemos|hay|esta|estan|quedo|armaste|arme|preparaste|prepare|elaboraste|descargue"
    r"|descargaste|recibi|guarde|guardaste|viste|vimos)"
)
_POSTERIOR_FUENTE = re.compile(
    r"^(?:\s+[^\s,.;:]+){0,2}?\s+(?:que\s+(?:(?:te|me|le|les|nos|se|ya|recien)\s+)*"
    + _VERBOS_PASADO
    + r"\b|adjunt[oa]s?\b)"
)

DIRECTIVA_EXCEL = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: Excel (.xlsx). Entrega el resultado como archivo Excel. "
    "No generes HTML ni dashboard, aunque antes se haya hecho, salvo que este mensaje lo pida.]"
)
DIRECTIVA_HTML = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: HTML interactivo. Entrega el resultado como HTML. "
    "No cambies a Excel u otro formato salvo que este mensaje lo pida.]"
)
DIRECTIVA_WORD = "[FORMATO PEDIDO EN ESTE MENSAJE: Word (.docx). No cambies a otro formato.]"
DIRECTIVA_PDF = "[FORMATO PEDIDO EN ESTE MENSAJE: PDF. No cambies a otro formato.]"
DIRECTIVA_CHAT = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: respuesta en el chat. El usuario no pidió un archivo: entrega el resumen y las "
    "tablas en Markdown dentro de la respuesta y no generes archivos (Excel, HTML, PDF, Word ni informe) salvo que "
    "lo pida. Si una tabla pasa de 40 filas, muestra las 40 más relevantes, di cuántas son en total y ofrece el Excel.]"
)
_FUENTE_PLANILLA = re.compile(r"\b(excel|xlsx|xls|csv|planillas?|hojas? de calculo)\b")
_PIDE_ENTREGABLE = re.compile(
    r"\b(informes?|reportes?|documentos?|presentacion(es)?|diapositivas?|laminas?|pptx?|powerpoint|graficos?|"
    r"archivos? (nuevos?|aparte)|descarga(ble|r)?|adjunt(a|alo|ame)|onedrive|sharepoint|correo|envia(lo|me)?|"
    r"corrig\w*|corrige\w*|actualiz\w*|edit\w*|modific\w*|agreg\w*|anad\w*|complet\w*|cambi\w*|elimin\w*|"
    r"quit\w*|borr\w*|reemplaz\w*|export\w*|convi[er]rt\w*|pas(a|alo|ala|ar)\s+a|gener\w*|cre(a|ar|ame|alo)|"
    r"crea\w*|arm(a|ar|ame|alo)|guard\w*|sub(e|ir|elo))\b"
)


def _es_fuente(texto: str, inicio: int, fin: int) -> bool:
    if inicio > 0 and texto[inicio - 1] == ".":
        return True
    antes = texto[max(0, inicio - 60) : inicio]
    if _PREVIO_FUENTE.search(antes):
        return True
    return bool(_PREVIO_DEFINIDO.search(antes) and _POSTERIOR_FUENTE.match(texto[fin : fin + 80]))


def _pide_formato(patron: re.Pattern, texto: str) -> bool:
    return any(not _es_fuente(texto, m.start(), m.end()) for m in patron.finditer(texto))


_PIDE_PROGRAMAR = re.compile(
    r"^\W*(?:(?:por favor|oye|hola|quiero|necesito|puedes|podrias)\s+)*"
    r"(?:programa|programar|programame|programas|agenda|agendar|agendame)\b(?:\W+\w+){0,4}?\W+"
    r"(?:tareas?|ejecuciones?|ejecucion|cron|recordatorios?)\b"
)


def directiva_formato(texto: str | None) -> str | None:
    if not texto:
        return None
    normalizado = " ".join(texto.translate(_SIN_TILDES).lower().split())
    if _PIDE_PROGRAMAR.search(normalizado):
        return None
    pedidos = [
        (_EXCEL, DIRECTIVA_EXCEL),
        (_HTML, DIRECTIVA_HTML),
        (_WORD, DIRECTIVA_WORD),
        (_PDF, DIRECTIVA_PDF),
    ]
    activos = [d for patron, d in pedidos if _pide_formato(patron, normalizado)]
    if not activos and _FUENTE_PLANILLA.search(normalizado) and not _PIDE_ENTREGABLE.search(normalizado):
        return DIRECTIVA_CHAT
    if len(activos) != 1:
        return None
    return activos[0]


def con_formato_pedido(texto: str | None, prompt: str) -> str:
    directiva = directiva_formato(texto)
    if not directiva:
        return prompt
    return f"{directiva}\n{prompt}"


_DIRECTIVA_AL_INICIO = re.compile(r"^\s*\[FORMATO PEDIDO EN ESTE MENSAJE: ([^.\]]+)")
_EXTENSIONES = {
    "excel": frozenset({".xlsx", ".xlsm", ".csv"}),
    "html": frozenset({".html", ".htm"}),
    "word": frozenset({".docx"}),
    "pdf": frozenset({".pdf"}),
}


def formato_de_la_directiva(prompt: str | None) -> str | None:
    coincidencia = _DIRECTIVA_AL_INICIO.match(prompt or "")
    if not coincidencia:
        return None
    nombre = coincidencia.group(1).strip().lower()
    for clave in ("excel", "html", "word", "pdf"):
        if nombre.startswith(clave):
            return clave
    return "chat" if nombre.startswith("respuesta en el chat") else None


def extensiones_pedidas(prompt: str | None) -> frozenset[str] | None:
    return _EXTENSIONES.get(formato_de_la_directiva(prompt) or "")
